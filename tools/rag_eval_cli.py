#!/usr/bin/env python3
"""RAG Evaluation Management CLI.

Commands:
  generate - Generate ground truth answers from nfcorpus dataset
  upload   - Upload nfcorpus documents as Nextcloud notes

Usage:
    # Generate ground truth
    uv run python tools/rag_eval_cli.py generate

    # Upload corpus to Nextcloud
    uv run python tools/rag_eval_cli.py upload --nextcloud-url http://localhost:8000 --username admin --password admin
"""

import io
import json
import sys
import zipfile
from pathlib import Path
from typing import Any

import anyio
import click
import httpx
from datasets import load_dataset
from httpx import BasicAuth

# Add parent directory to path to import from tests/
sys.path.insert(0, str(Path(__file__).parent.parent))

from nextcloud_mcp_server.client import NextcloudClient
from tests.rag_evaluation.llm_providers import create_llm_provider

# Paths
FIXTURES_DIR = Path(__file__).parent.parent / "tests" / "rag_evaluation" / "fixtures"
CORPUS_DIR = FIXTURES_DIR / "nfcorpus"
GROUND_TRUTH_FILE = FIXTURES_DIR / "ground_truth.json"
NOTE_MAPPING_FILE = FIXTURES_DIR / "note_mapping.json"

# Dataset URL
NFCORPUS_URL = (
    "https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/nfcorpus.zip"
)

# Selected test queries (from ADR-013)
SELECTED_QUERIES = [
    "PLAIN-2630",  # Alkylphenol Endocrine Disruptors and Allergies
    "PLAIN-2660",  # How Long to Detox From Fish Before Pregnancy?
    "PLAIN-2510",  # Coffee and Artery Function
    "PLAIN-2430",  # Preventing Brain Loss with B Vitamins?
    "PLAIN-2690",  # Chronic Headaches and Pork Tapeworms
]


def ensure_corpus_downloaded(force_download: bool = False) -> Path:
    """Ensure nfcorpus dataset is downloaded to fixtures directory.

    Args:
        force_download: Force re-download even if corpus exists

    Returns:
        Path to corpus directory

    Raises:
        RuntimeError: If download fails
    """
    if CORPUS_DIR.exists() and not force_download:
        click.echo(f"Corpus already exists at {CORPUS_DIR}")
        return CORPUS_DIR

    click.echo(f"Downloading nfcorpus dataset to {CORPUS_DIR}...")

    # Create fixtures directory
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

    # Download using HuggingFace datasets library (handles caching)
    try:
        # Download corpus
        click.echo("  Downloading corpus...")
        corpus_dataset = load_dataset(
            "BeIR/nfcorpus",
            "corpus",
            split="corpus",
        )

        # Download queries
        click.echo("  Downloading queries...")
        queries_dataset = load_dataset(
            "BeIR/nfcorpus",
            "queries",
            split="queries",
        )

        # Save to local fixtures directory as JSONL
        CORPUS_DIR.mkdir(parents=True, exist_ok=True)

        # Save corpus
        with open(CORPUS_DIR / "corpus.jsonl", "w") as f:
            for doc in corpus_dataset:
                f.write(json.dumps(doc) + "\n")

        # Save queries
        with open(CORPUS_DIR / "queries.jsonl", "w") as f:
            for query in queries_dataset:
                f.write(json.dumps(query) + "\n")

        # Download qrels from BEIR directly (not available via HuggingFace)
        click.echo("  Downloading qrels from BEIR ZIP...")
        with httpx.Client(timeout=300.0) as client:
            response = client.get(NFCORPUS_URL)
            response.raise_for_status()

            # Extract qrels from ZIP
            with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
                # The qrels are in nfcorpus/qrels/test.tsv within the ZIP
                qrels_path = "nfcorpus/qrels/test.tsv"
                qrels_dir = CORPUS_DIR / "qrels"
                qrels_dir.mkdir(parents=True, exist_ok=True)

                qrels_content = zf.read(qrels_path).decode("utf-8")
                with open(qrels_dir / "test.tsv", "w") as f:
                    f.write(qrels_content)

        click.echo(f"Dataset downloaded to {CORPUS_DIR}")
        return CORPUS_DIR

    except Exception as e:
        raise RuntimeError(f"Failed to download nfcorpus dataset: {e}") from e


def load_corpus(corpus_dir: Path) -> dict[str, dict]:
    """Load corpus documents from local directory.

    Args:
        corpus_dir: Path to corpus directory

    Returns:
        Dict mapping document ID to document data
    """
    corpus = {}
    with open(corpus_dir / "corpus.jsonl") as f:
        for line in f:
            doc = json.loads(line)
            corpus[doc["_id"]] = doc
    return corpus


def load_queries(corpus_dir: Path) -> dict[str, dict]:
    """Load queries from local directory.

    Args:
        corpus_dir: Path to corpus directory

    Returns:
        Dict mapping query ID to query data
    """
    queries = {}
    with open(corpus_dir / "queries.jsonl") as f:
        for line in f:
            query = json.loads(line)
            queries[query["_id"]] = query
    return queries


def load_qrels(corpus_dir: Path) -> dict[str, list[tuple[str, int]]]:
    """Load query relevance judgments from local directory.

    Args:
        corpus_dir: Path to corpus directory

    Returns:
        Dict mapping query ID to list of (doc_id, score) tuples
    """
    qrels: dict[str, list[tuple[str, int]]] = {}
    with open(corpus_dir / "qrels" / "test.tsv") as f:
        next(f)  # Skip header
        for line in f:
            query_id, corpus_id, score = line.strip().split("\t")
            if query_id not in qrels:
                qrels[query_id] = []
            qrels[query_id].append((corpus_id, int(score)))

    # Sort by score descending
    for query_id in qrels:
        qrels[query_id].sort(key=lambda x: x[1], reverse=True)

    return qrels


async def generate_ground_truth_answer(
    query_text: str, relevant_docs: list[dict[str, Any]], llm
) -> str:
    """Generate ground truth answer from highly relevant documents.

    Args:
        query_text: The query/question
        relevant_docs: List of highly relevant documents (top 5)
        llm: LLM provider instance

    Returns:
        Generated ground truth answer
    """
    # Construct context from documents
    context_parts = []
    for i, doc in enumerate(relevant_docs, 1):
        context_parts.append(
            f"Document {i}:\nTitle: {doc['title']}\nText: {doc['text']}\n"
        )
    context = "\n".join(context_parts)

    # Generate ground truth
    prompt = f"""Based on the following medical/biomedical documents, provide a comprehensive, factual answer to this question.

Question: {query_text}

{context}

Instructions:
- Provide a clear, well-structured answer that synthesizes information from the documents
- Focus on accuracy and completeness
- Use specific facts and findings from the documents
- Keep the answer concise but informative (2-4 paragraphs)
- Do not make up information not present in the documents

Answer:"""

    click.echo(f"  Generating answer for: {query_text}")
    answer = await llm.generate(prompt, max_tokens=500)
    click.echo(f"  Generated {len(answer)} characters")
    return answer.strip()


@click.group()
def cli():
    """RAG Evaluation Management CLI.

    Manage ground truth generation and corpus upload for RAG evaluation tests.
    """
    pass


@cli.command()
@click.option(
    "--provider",
    type=click.Choice(["ollama", "anthropic"]),
    default="ollama",
    help="LLM provider to use for generation",
)
@click.option(
    "--model",
    help="Model name (default: llama3.2:1b for Ollama, claude-3-5-sonnet-20241022 for Anthropic)",
)
@click.option(
    "--force-download",
    is_flag=True,
    help="Force re-download of nfcorpus dataset",
)
def generate(provider: str, model: str | None, force_download: bool):
    """Generate ground truth answers for RAG evaluation.

    This command:
    1. Downloads nfcorpus dataset (if not already cached)
    2. For each selected query, extracts highly relevant documents
    3. Uses an LLM to synthesize a reference answer
    4. Saves ground truth to fixtures/ground_truth.json

    Environment variables:
      RAG_EVAL_PROVIDER: Provider type (ollama or anthropic)
      RAG_EVAL_OLLAMA_BASE_URL: Ollama base URL
      RAG_EVAL_OLLAMA_MODEL: Ollama model name
      RAG_EVAL_ANTHROPIC_API_KEY: Anthropic API key
      RAG_EVAL_ANTHROPIC_MODEL: Anthropic model name
    """

    async def _generate():
        click.echo("=" * 80)
        click.echo("RAG Ground Truth Generation")
        click.echo("=" * 80)

        # Ensure corpus is downloaded
        corpus_dir = ensure_corpus_downloaded(force_download)

        # Load dataset
        click.echo("\nLoading nfcorpus dataset...")
        corpus = load_corpus(corpus_dir)
        queries = load_queries(corpus_dir)
        qrels = load_qrels(corpus_dir)
        click.echo(f"Loaded {len(corpus)} documents, {len(queries)} queries")

        # Create LLM provider
        click.echo("\nInitializing LLM provider...")
        try:
            llm = create_llm_provider(
                provider=provider,
                ollama_model=model if provider == "ollama" else None,
                anthropic_model=model if provider == "anthropic" else None,
            )
            provider_type = type(llm).__name__
            click.echo(f"Using provider: {provider_type}")
        except ValueError as e:
            click.echo(f"\nError: {e}", err=True)
            return 1

        # Generate ground truth for each selected query
        ground_truth_data = []

        try:
            for query_id in SELECTED_QUERIES:
                if query_id not in queries:
                    click.echo(
                        f"\nWarning: Query {query_id} not found in dataset", err=True
                    )
                    continue

                query = queries[query_id]
                query_text = query["text"]

                # Get highly relevant documents (score=2)
                if query_id not in qrels:
                    click.echo(
                        f"\nWarning: No relevance judgments for {query_id}", err=True
                    )
                    continue

                highly_relevant_doc_ids = [
                    doc_id for doc_id, score in qrels[query_id] if score == 2
                ]

                if not highly_relevant_doc_ids:
                    click.echo(
                        f"\nWarning: No highly relevant docs for {query_id}", err=True
                    )
                    continue

                # Get top 5 highly relevant documents
                relevant_docs = []
                for doc_id in highly_relevant_doc_ids[:5]:
                    if doc_id in corpus:
                        relevant_docs.append(corpus[doc_id])

                if not relevant_docs:
                    click.echo(
                        f"\nWarning: Could not load documents for {query_id}", err=True
                    )
                    continue

                # Generate ground truth answer
                click.echo(f"\n{'-' * 80}")
                ground_truth_answer = await generate_ground_truth_answer(
                    query_text, relevant_docs, llm
                )

                # Store result
                ground_truth_data.append(
                    {
                        "query_id": query_id,
                        "query_text": query_text,
                        "ground_truth_answer": ground_truth_answer,
                        "expected_document_ids": highly_relevant_doc_ids,
                        "highly_relevant_count": len(highly_relevant_doc_ids),
                    }
                )

                click.echo(f"  Preview: {ground_truth_answer[:200]}...")

        finally:
            await llm.close()

        # Save ground truth
        GROUND_TRUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(GROUND_TRUTH_FILE, "w") as f:
            json.dump(ground_truth_data, f, indent=2)

        click.echo(f"\n{'=' * 80}")
        click.echo(f"Generated {len(ground_truth_data)} ground truth answers")
        click.echo(f"Saved to: {GROUND_TRUTH_FILE}")
        click.echo("=" * 80)

        return 0

    sys.exit(anyio.run(_generate))


@cli.command()
@click.option(
    "--nextcloud-url",
    envvar="NEXTCLOUD_HOST",
    required=True,
    help="Nextcloud base URL (e.g., http://localhost:8000)",
)
@click.option(
    "--username",
    envvar="NEXTCLOUD_USERNAME",
    required=True,
    help="Nextcloud username",
)
@click.option(
    "--password",
    envvar="NEXTCLOUD_PASSWORD",
    required=True,
    help="Nextcloud password",
)
@click.option(
    "--category",
    default="nfcorpus_rag_eval",
    help="Category/folder for uploaded notes",
)
@click.option(
    "--force-download",
    is_flag=True,
    help="Force re-download of nfcorpus dataset",
)
@click.option(
    "--force",
    is_flag=True,
    help="Delete all existing notes in the target category before uploading",
)
def upload(
    nextcloud_url: str,
    username: str,
    password: str,
    category: str,
    force_download: bool,
    force: bool,
):
    """Upload nfcorpus corpus documents as Nextcloud notes.

    This command:
    1. Downloads nfcorpus dataset (if not already cached)
    2. Optionally deletes existing notes in target category (--force)
    3. Uploads all corpus documents as Nextcloud notes
    4. Saves document ID → note ID mapping to fixtures/note_mapping.json

    The note mapping file is used by pytest tests to map expected document IDs
    to actual note IDs in Nextcloud.
    """

    async def _upload():
        click.echo("=" * 80)
        click.echo("Upload nfcorpus Corpus to Nextcloud")
        click.echo("=" * 80)

        # Ensure corpus is downloaded
        corpus_dir = ensure_corpus_downloaded(force_download)

        # Load corpus
        click.echo("\nLoading corpus...")
        corpus = load_corpus(corpus_dir)
        click.echo(f"Loaded {len(corpus)} documents")

        # Create Nextcloud client
        click.echo(f"\nConnecting to Nextcloud at {nextcloud_url}...")
        nc_client = NextcloudClient(
            base_url=nextcloud_url,
            username=username,
            auth=BasicAuth(username, password),
        )

        try:
            # Delete existing notes in category if force is specified
            if force:
                click.echo(
                    f"\n--force specified: Deleting existing notes in category '{category}'..."
                )

                # Collect notes to delete
                notes_to_delete = []
                async for note in nc_client.notes.get_all_notes():
                    if note.get("category") == category:
                        notes_to_delete.append(note["id"])

                if not notes_to_delete:
                    click.echo(f"No existing notes found in category '{category}'")
                else:
                    click.echo(f"Found {len(notes_to_delete)} notes to delete")

                    deleted_count = 0
                    delete_errors = []
                    delete_semaphore = anyio.Semaphore(20)

                    async def delete_note(note_id: int):
                        """Delete a single note."""
                        nonlocal deleted_count

                        async with delete_semaphore:
                            try:
                                await nc_client.notes.delete_note(note_id)
                                deleted_count += 1
                                if deleted_count % 100 == 0:
                                    click.echo(f"  Deleted {deleted_count} notes...")
                            except Exception as e:
                                error_msg = f"Error deleting note {note_id}: {e}"
                                delete_errors.append(error_msg)
                                click.echo(f"  {error_msg}", err=True)

                    # Delete all notes concurrently
                    async with anyio.create_task_group() as tg:
                        for note_id in notes_to_delete:
                            tg.start_soon(delete_note, note_id)

                    click.echo(
                        f"Deleted {deleted_count} existing notes in category '{category}'"
                    )
                    if delete_errors:
                        click.echo(
                            f"Encountered {len(delete_errors)} errors during deletion",
                            err=True,
                        )

            # Upload documents concurrently
            click.echo(f"\nUploading {len(corpus)} documents as notes (concurrent)...")
            click.echo(f"Category: {category}")

            note_mapping = {}
            uploaded_count = 0
            upload_errors = []

            # Semaphore to limit concurrent uploads (avoid overwhelming server)
            max_concurrent = 20
            semaphore = anyio.Semaphore(max_concurrent)

            async def upload_document(doc_id: str, doc: dict[str, Any]):
                """Upload a single document as a note."""
                nonlocal uploaded_count

                async with semaphore:
                    title = f"[{doc_id}] {doc['title'][:100]}"  # Truncate long titles
                    content = doc["text"]

                    try:
                        note_data = await nc_client.notes.create_note(
                            title=title,
                            content=content,
                            category=category,
                        )

                        # Store mapping
                        note_id = note_data["id"]
                        note_mapping[doc_id] = note_id

                        uploaded_count += 1

                        # Progress indicator every 100 docs
                        if uploaded_count % 100 == 0:
                            click.echo(
                                f"  Uploaded {uploaded_count}/{len(corpus)} documents..."
                            )

                    except Exception as e:
                        error_msg = f"Error uploading {doc_id}: {e}"
                        upload_errors.append(error_msg)
                        click.echo(f"  {error_msg}", err=True)

            # Upload all documents concurrently using task group
            async with anyio.create_task_group() as tg:
                for doc_id, doc in corpus.items():
                    tg.start_soon(upload_document, doc_id, doc)

            click.echo(f"\nUploaded {uploaded_count} documents successfully")
            if upload_errors:
                click.echo(
                    f"Encountered {len(upload_errors)} errors during upload", err=True
                )

            # Save note mapping
            with open(NOTE_MAPPING_FILE, "w") as f:
                json.dump(note_mapping, f, indent=2)

            click.echo(f"Saved note mapping to: {NOTE_MAPPING_FILE}")
            click.echo(f"  Mapped {len(note_mapping)} document IDs to note IDs")

        finally:
            # Close the Nextcloud client
            await nc_client.close()

        click.echo("=" * 80)
        click.echo("Upload complete!")
        click.echo("=" * 80)

        return 0

    sys.exit(anyio.run(_upload))


if __name__ == "__main__":
    cli()
