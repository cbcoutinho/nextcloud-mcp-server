# Introducing Astrolabe: Navigate Your Data Universe in Nextcloud

Your Nextcloud instance holds years of notes, projects, recipes, contacts, and documents. But when you need to find something, you're stuck typing exact keywords and hoping for the best. Search "car repair" and miss that note titled "Vehicle maintenance tips." Search "meeting agenda" and overlook the calendar event called "Team sync." Traditional keyword search demands that you remember exactly how you wrote things down.

What if your search could understand what you *mean*, not just what you type?

Meet **Astrolabe**—a Nextcloud app that brings AI-powered semantic search to your self-hosted cloud. Named after the ancient navigational instrument that helped travelers chart courses by the stars, Astrolabe helps you navigate your personal knowledge by mapping the semantic connections between your documents.

## The Astrolabe Metaphor

The astrolabe was one of humanity's most elegant scientific instruments—an analog computer for solving problems related to time and the position of celestial bodies. Its theoretical foundation traces back to **Hipparchus of Nicaea** (c. 190–120 BCE), who discovered the stereographic projection that allows a three-dimensional celestial sphere to be represented on a flat surface. Later Greek scholars like **Theon of Alexandria** and his daughter **Hypatia** refined it into a practical instrument, and during the Islamic Golden Age, astronomers in Baghdad, Damascus, and Cordoba perfected its design and applications.

For nearly two millennia, astrolabes served astronomers, navigators, scholars, and religious officials across the Greek, Byzantine, Islamic, and medieval European worlds. These instruments allowed users to determine time, find celestial positions, calculate daylight hours, identify constellations, and even determine the direction of Mecca for prayer—all without complex calculations. The astrolabe made the vast complexity of the heavens understandable and navigable.

**Astrolabe** (the app) does the same for your data. Every document, note, and calendar event becomes a point of light in your personal data universe. The app maps their semantic relationships—their meaning, not just their words—and suddenly the connections become visible. Documents cluster by topic, related ideas sit nearby, and you can navigate this landscape as naturally as medieval scholars once read the stars. Where the original astrolabe projected the celestial sphere onto brass, this one projects your knowledge into explorable semantic space.

## Semantic Search: Find Meaning, Not Just Keywords

The core feature of Astrolabe is semantic search. Instead of matching exact keywords, it understands the concepts in your query and finds related content.

**What this looks like in practice:**

| You Search For | Traditional Search Finds | Astrolabe Also Finds |
|----------------|--------------------------|----------------------|
| "car repair" | Documents containing "car repair" | Notes about "vehicle maintenance," "fixing the truck" |
| "team planning" | Documents with "team planning" | Calendar events titled "Q2 kickoff," Deck cards about "project roadmap" |
| "pasta recipes" | Documents with "pasta recipes" | Notes about "Italian cooking," "homemade noodles," "carbonara tips" |

This works across multiple Nextcloud apps: Notes, Files (including PDFs with OCR), Deck cards, Calendar events, Contacts, and News/RSS items. One search bar, all your content, understood by meaning.

### Hybrid Search: Best of Both Worlds

Sometimes you want exact matches ("PROJ-2024-001"), sometimes you want semantic understanding ("that project from last year about authentication"). Astrolabe's hybrid search combines both approaches:

- **Semantic search** uses embeddings to find conceptually related content
- **BM25 keyword search** finds exact matches and important terms
- **Reciprocal Rank Fusion (RRF)** intelligently merges the results

You can adjust the balance or switch modes entirely depending on your needs.

![Unified Search Integration](https://github.com/cbcoutinho/nextcloud-mcp-server/blob/master/third_party/astrolabe/screenshots/01-unified-search-astrolabe.png?raw=1)
*Astrolabe results appear alongside traditional search in Nextcloud's unified search bar*

## Visualize Your Data Universe

Beyond search, Astrolabe includes an interactive 3D visualization that shows your documents positioned in semantic space. Similar documents cluster together. Topics form constellations. You can rotate, zoom, and explore.

This isn't just eye candy—it's a practical tool for knowledge discovery:

- **Find forgotten connections**: Search for your current project and watch as related documents from months ago light up nearby
- **Spot topic clusters**: See how your notes naturally group by subject
- **Explore the unknown**: Click on points near your search results to discover content you didn't know was related

The visualization uses Principal Component Analysis (PCA) to project high-dimensional embeddings (768 dimensions) down to 3D space while preserving the relationships between documents. We implemented a lightweight, custom PCA specifically for this—no heavyweight ML libraries required.

![3D Vector Visualization](https://github.com/cbcoutinho/nextcloud-mcp-server/blob/master/third_party/astrolabe/screenshots/02-semantic-search-with-plot.png?raw=1)
*Documents cluster by semantic similarity. The query point (red) shows your search, and related documents cluster nearby*

## Power Your AI Agents

Astrolabe isn't just for humans—it's for your AI assistants too.

The backend runs a **Model Context Protocol (MCP)** server, which means AI tools like Claude Desktop, Cursor, or custom agents can connect directly to your Nextcloud data. Your AI assistant can:

- Search your notes semantically ("Find everything related to the Kubernetes migration")
- Retrieve document content for context
- Get AI-generated answers with citations from your documents (RAG)

The critical point: **your data never leaves your infrastructure**. The MCP server runs on your hardware. Your AI assistant sends queries, the server returns results, and you maintain full control. No documents uploaded to third-party services.

### Retrieval-Augmented Generation (RAG)

Ask a question, and Astrolabe can retrieve relevant documents and have your AI synthesize an answer—complete with citations:

```
You: "What were the main issues we had deploying to production last month?"

Astrolabe finds: 3 relevant notes, 2 Deck cards, 1 calendar event

AI generates: "Based on your documents, there were three main issues:
1. Database migration timeout (see Note: 'Prod deploy 2024-01-15')
2. SSL certificate renewal (see Deck card: 'Ops Tasks')
3. Resource limits on the new pods (see Note: 'K8s troubleshooting')
```

This uses MCP's sampling capability—the server doesn't run its own LLM. Instead, it asks your client's AI to generate the response. You choose the model, you control the costs.

## Under the Hood

For the technically curious, here's how Astrolabe works:

### Embedding Providers

Astrolabe supports multiple backends for generating semantic embeddings:

- **Amazon Bedrock**: Enterprise-grade, Titan embeddings
- **OpenAI**: Direct OpenAI API or compatible endpoints (including GitHub Models)
- **Ollama**: Self-hosted, privacy-focused, runs entirely on your hardware

The system auto-detects available providers based on environment variables and falls back gracefully. Deploy Ollama on your server for full privacy, or use Bedrock for enterprise scale—same codebase, zero code changes.

### Background Indexing

Documents are indexed automatically via webhooks. When you create or edit a note, Nextcloud fires an event, and the MCP server processes it in the background. No manual sync required.

The indexing pipeline:
1. **Scanner** detects changes via ETags and modification timestamps
2. **Queue** manages backpressure (up to 10k pending documents)
3. **Worker pool** processes embeddings concurrently (configurable, default 3 workers)
4. **Qdrant** stores vectors for fast similarity search

### Lightweight by Design

We deliberately avoided heavyweight dependencies:

- **Custom PCA**: No scikit-learn, just efficient eigendecomposition
- **In-process async**: No separate message queues or worker processes—just anyio TaskGroups
- **Plugin architecture**: New apps (Notes, Calendar, etc.) are simple scanner/processor implementations

This means Astrolabe runs comfortably alongside your Nextcloud on modest hardware.

```
┌──────────────┐     ┌─────────────┐     ┌─────────┐
│   Nextcloud  │────▶│ MCP Server  │────▶│ Qdrant  │
│ (Astrolabe)  │◀────│  (Python)   │◀────│ (Vectors)│
└──────────────┘     └─────────────┘     └─────────┘
       │                    │
       │ OAuth/Token        │ Embeddings
       ▼                    ▼
   ┌────────┐         ┌──────────┐
   │  User  │         │ Ollama/  │
   │Browser │         │ Bedrock  │
   └────────┘         └──────────┘
```

## Getting Started

### Requirements

- Nextcloud 31 or 32
- MCP server instance (Docker recommended)
- Vector database (Qdrant, included in Docker setup)
- Embedding provider (Ollama for self-hosted, or cloud options)

### Quick Setup

1. **Install the Astrolabe app** from the Nextcloud App Store (or manually)

2. **Start the MCP server** (Docker Compose makes this easy):
   ```bash
   docker compose up -d mcp qdrant ollama
   ```

3. **Configure the connection** in your Nextcloud `config.php`:
   ```php
   'astrolabe' => [
       'mcp_server_url' => 'http://localhost:8000',
   ],
   ```

4. **Authorize access** in Settings → Personal → Astrolabe

5. **Start searching** using Nextcloud's unified search bar

For detailed setup instructions, including OAuth configuration and embedding provider options, see the [documentation](https://github.com/cbcoutinho/nextcloud-mcp-server).

## What Can You Index?

Astrolabe currently supports:

| App | What Gets Indexed |
|-----|-------------------|
| **Notes** | Full text and metadata |
| **Files** | PDFs (with OCR), DOCX, text files |
| **Deck** | Card titles and descriptions |
| **Calendar** | Event titles, descriptions, and details |
| **Contacts** | Names, notes, and contact information |
| **News** | RSS/Atom feed articles |

Each result shows the document type, relevance score, and a direct link to the source. For large documents, it shows which chunk (section) matched.

![Chunk Viewer](https://github.com/cbcoutinho/nextcloud-mcp-server/blob/master/third_party/astrolabe/screenshots/03-chunk-viewer-open.png?raw=1)
*Click a result to see the matching chunk in context*

## Who Is This For?

**Researchers and students**: Find all notes related to your thesis topic, even when you used different terminology across semesters. Discover connections between papers you read months apart.

**Teams and organizations**: Surface institutional knowledge that would otherwise stay buried. New team members can search for concepts instead of knowing exactly what to look for.

**Developers**: Connect your AI coding assistant to your Nextcloud. Give it access to project notes, meeting records, and documentation without copy-pasting context.

**Personal knowledge managers**: Discover forgotten documents related to your current work. Watch your knowledge base evolve over time through the visualization.

## Try It Out

Astrolabe is open source (AGPL) and ready to use. Your data universe has been waiting in the dark—it's time to turn on the lights.

- **Install**: [Nextcloud App Store](https://apps.nextcloud.com/apps/astrolabe)
- **Source**: [GitHub](https://github.com/cbcoutinho/nextcloud-mcp-server)
- **Documentation**: [Setup Guide](https://github.com/cbcoutinho/nextcloud-mcp-server/tree/master/docs)
- **Issues**: [Report bugs or request features](https://github.com/cbcoutinho/nextcloud-mcp-server/issues)

---

*Astrolabe is maintained by [Chris Coutinho](https://github.com/cbcoutinho). Contributions welcome.*
