"""ADR-042 step 4b: image points at ingest.

Covers the worker-side pieces: which files take part, downscaling to the
model's limits, the skip/retry classification, the dead-letter key, and what
``_index_document`` writes for an image file. The dual-backend prune test pins
that ``prune_stale_chunks`` never reaches the image point.
"""

from __future__ import annotations

import dataclasses
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image
from qdrant_client import AsyncQdrantClient, models

from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.document_processors import get_registry
from nextcloud_mcp_server.document_processors.base import ProcessingResult
from nextcloud_mcp_server.document_processors.escalation import (
    escalation_tiers_signature,
)
from nextcloud_mcp_server.document_source import MemoryDocumentSource
from nextcloud_mcp_server.providers.base import ImageInputLimits, ImageInputRejected
from nextcloud_mcp_server.vector import image_ingest, payload_keys, processor
from nextcloud_mcp_server.vector.scanner import DocumentTask

pytestmark = pytest.mark.unit

PNG = "image/png"


def _png(width: int = 8, height: int = 8) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _limits(**kw) -> ImageInputLimits:
    return ImageInputLimits(
        mime_types=frozenset({"image/png", "image/jpeg", "image/gif"}), **kw
    )


class TestFit:
    def test_within_limits_passes_the_bytes_through(self):
        data = _png()
        image = image_ingest._fit(data, PNG, _limits(max_bytes=10**6, max_pixels=10**6))
        assert (image.data, image.mime_type) == (data, PNG)

    def test_over_pixels_downscales_under_the_limit(self):
        image = image_ingest._fit(_png(400, 300), PNG, _limits(max_pixels=10_000))
        with Image.open(io.BytesIO(image.data)) as out:
            assert out.width * out.height <= 10_000
        assert image.mime_type == "image/jpeg"

    def test_over_bytes_shrinks_under_the_limit(self):
        noisy = Image.effect_noise((600, 600), 100).convert("RGB")
        buf = io.BytesIO()
        noisy.save(buf, format="PNG")
        image = image_ingest._fit(buf.getvalue(), PNG, _limits(max_bytes=20_000))
        assert len(image.data) <= 20_000

    def test_animated_gif_is_reencoded_from_its_first_frame(self):
        frames = [Image.new("RGB", (8, 8), c) for c in ("red", "blue")]
        buf = io.BytesIO()
        frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:])
        image = image_ingest._fit(buf.getvalue(), "image/gif", _limits())
        assert image.mime_type == "image/jpeg"
        with Image.open(io.BytesIO(image.data)) as out:
            r, g, b = out.convert("RGB").getpixel((4, 4))
            assert r > 200 and b < 60  # red: frame 0, not frame 1

    def test_over_pillows_pixel_ceiling_is_refused(self, monkeypatch):
        """Pillow only warns between 1x and 2x its ceiling; _fit refuses."""
        monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 50)
        with pytest.raises(Image.DecompressionBombError):
            image_ingest._fit(_png(), PNG, _limits())


class TestEmbedDocumentImage:
    @pytest.fixture
    def provider(self, mocker):
        prov = SimpleNamespace(
            image_input_limits=_limits(max_bytes=10**6),
            embed_image=AsyncMock(return_value=[0.5, 0.5]),
        )
        mocker.patch.object(image_ingest, "get_provider", lambda: prov)
        return prov

    def _settings(self, cap: int = 10**6):
        return SimpleNamespace(
            image_embedding_max_source_bytes=cap, image_embedding_model="img-m"
        )

    async def test_embeds(self, provider):
        source = MemoryDocumentSource(_png(), PNG, "/a.png")
        assert await image_ingest.embed_document_image(
            source, PNG, self._settings()
        ) == ([0.5, 0.5], None)

    async def test_over_the_source_cap_is_skipped_unread(self, provider):
        source = MemoryDocumentSource(_png(), PNG, "/a.png")
        result = await image_ingest.embed_document_image(
            source, PNG, self._settings(10)
        )
        assert result == (None, image_ingest.SKIP_TOO_LARGE)
        provider.embed_image.assert_not_awaited()

    async def test_undecodable_is_skipped_without_a_paid_call(self, provider):
        source = MemoryDocumentSource(b"not an image", PNG, "/a.png")
        result = await image_ingest.embed_document_image(source, PNG, self._settings())
        assert result == (None, image_ingest.SKIP_REJECTED)
        provider.embed_image.assert_not_awaited()

    async def test_unconvertible_mode_is_a_skip_not_a_retry(self, provider, mocker):
        mocker.patch.object(image_ingest, "_fit", side_effect=ValueError("mode"))
        source = MemoryDocumentSource(_png(), PNG, "/a.png")
        result = await image_ingest.embed_document_image(source, PNG, self._settings())
        assert result == (None, image_ingest.SKIP_REJECTED)

    async def test_provider_rejection_is_a_skip(self, provider):
        provider.embed_image.side_effect = ImageInputRejected("invalid_image", 0)
        source = MemoryDocumentSource(_png(), PNG, "/a.png")
        result = await image_ingest.embed_document_image(source, PNG, self._settings())
        assert result == (None, image_ingest.SKIP_REJECTED)

    async def test_transient_errors_raise_so_the_job_retries(self, provider):
        provider.embed_image.side_effect = TimeoutError()
        source = MemoryDocumentSource(_png(), PNG, "/a.png")
        with pytest.raises(TimeoutError):
            await image_ingest.embed_document_image(source, PNG, self._settings())


class TestScope:
    def test_off_until_the_collection_is_ready(self, mocker):
        mocker.patch.object(image_ingest, "image_vector_ready", lambda: False)
        assert image_ingest.image_mime_types() == frozenset()

    def test_accepted_types_narrowed_to_what_pillow_decodes(self, mocker):
        mocker.patch.object(image_ingest, "image_vector_ready", lambda: True)
        limits = ImageInputLimits(mime_types=frozenset({PNG, "image/heic"}))
        mocker.patch.object(
            image_ingest,
            "get_provider",
            lambda: SimpleNamespace(image_input_limits=limits),
        )
        assert image_ingest.image_mime_types() == {PNG}
        assert image_ingest.in_image_scope("image/png; q=1", "hybrid")
        assert not image_ingest.in_image_scope(PNG, "keyword")


class TestTiersSignature:
    def _settings(self, model: str = "img-a"):
        return dataclasses.replace(get_settings(), image_embedding_model=model)

    def test_non_images_never_carry_the_image_model(self):
        a = escalation_tiers_signature(
            self._settings("img-a"), file_path="/x.pdf", index_mode="hybrid"
        )
        b = escalation_tiers_signature(
            self._settings("img-b"), file_path="/x.pdf", index_mode="hybrid"
        )
        assert a == b == escalation_tiers_signature(self._settings())

    def test_images_change_with_the_model_and_the_mode(self):
        sig = escalation_tiers_signature
        base = sig(self._settings("img-a"), file_path="/p.png", index_mode="hybrid")
        assert base.endswith(";img=img-a;mode=hybrid")
        assert base != sig(
            self._settings("img-b"), file_path="/p.png", index_mode="hybrid"
        )
        assert base != sig(
            self._settings("img-a"), file_path="/p.png", index_mode="keyword"
        )


async def test_prune_spares_the_image_point_but_document_delete_does_not():
    """prune_stale_chunks skips the image point (no chunk_index); doc deletes don't.

    Local mode here; tests/integration/test_image_ingest_qdrant_server.py
    runs the same on a real server, since the two have differed on filters.
    """
    client = AsyncQdrantClient(location=":memory:")
    await client.create_collection(
        "c",
        vectors_config={
            "dense": models.VectorParams(size=2, distance=models.Distance.COSINE),
            "image": models.VectorParams(size=2, distance=models.Distance.COSINE),
        },
    )
    doc = {"doc_id": "7", "doc_type": "file"}
    image_id = payload_keys.image_point_id("7")
    await client.upsert(
        "c",
        points=[
            models.PointStruct(
                id=1, vector={"dense": [1, 0]}, payload={**doc, "chunk_index": 0}
            ),
            models.PointStruct(
                id=2, vector={"dense": [1, 0]}, payload={**doc, "chunk_index": 1}
            ),
            models.PointStruct(
                id=image_id,
                vector={"image": [0, 1]},
                payload={
                    **doc,
                    payload_keys.VECTOR_KIND: payload_keys.VECTOR_KIND_IMAGE,
                },
            ),
        ],
    )

    await processor.prune_stale_chunks(
        client, collection_name="c", doc_id="7", doc_type="file", kept_chunks=1
    )
    points, _ = await client.scroll("c", limit=10)
    assert {str(p.id) for p in points} == {"1", image_id}

    await client.delete(
        "c",
        points_selector=models.Filter(
            must=[
                models.FieldCondition(key=k, match=models.MatchValue(value=v))
                for k, v in doc.items()
            ]
        ),
    )
    assert (await client.count("c")).count == 0
    await client.close()


class TestIndexDocument:
    """What ``_index_document`` writes for an image file (Qdrant mocked)."""

    @pytest.fixture
    def env(self, mocker):
        settings = dataclasses.replace(
            get_settings(),
            image_embedding_model="img-m",
            document_stream_download_enabled=False,
            usage_metering_enabled=False,
        )
        mocker.patch.object(processor, "get_settings", lambda: settings)
        mocker.patch.object(
            processor, "claim_existing_index", AsyncMock(return_value=False)
        )
        mocker.patch.object(
            processor, "existing_principals", AsyncMock(return_value=[])
        )
        mocker.patch.object(processor, "delete_placeholder_point", AsyncMock())
        mocker.patch.object(processor, "clear_dead_letter", AsyncMock())
        mocker.patch.object(processor, "image_vector_ready", lambda: True)
        mocker.patch.object(
            processor, "in_image_scope", lambda _ct, mode: mode == "hybrid"
        )
        mocker.patch(
            "nextcloud_mcp_server.vector.folder_ancestors.resolve_folder_ancestors",
            AsyncMock(return_value=[]),
        )
        # No text processor reads PNG in this setup.
        mocker.patch.object(get_registry(), "find_processor", return_value=None)
        nc = MagicMock()
        nc.webdav.read_file = AsyncMock(return_value=(_png(), PNG, None))
        return SimpleNamespace(
            settings=settings,
            nc=nc,
            qdrant=SimpleNamespace(upsert=AsyncMock(), delete=AsyncMock()),
            mark=mocker.patch.object(processor, "mark_dead_letter", AsyncMock()),
            embed=mocker.patch.object(
                processor,
                "embed_document_image",
                AsyncMock(return_value=([0.5, 0.5], None)),
            ),
        )

    def _task(self, mode: str = "hybrid") -> DocumentTask:
        return DocumentTask(
            user_id="alice",
            doc_id="7",
            doc_type="file",
            operation="index",
            modified_at=1,
            file_path="/Photos/p.png",
            etag="e1",
            index_mode=mode,
        )

    async def test_image_only_file_writes_one_full_payload_image_point(self, env):
        await processor._index_document(self._task(), env.nc, env.qdrant, tier=None)

        (call,) = env.qdrant.upsert.await_args_list
        (point,) = call.kwargs["points"]
        assert point.id == payload_keys.image_point_id("7")
        assert point.vector == {"image": [0.5, 0.5]}
        payload = point.payload
        assert payload[payload_keys.VECTOR_KIND] == payload_keys.VECTOR_KIND_IMAGE
        assert payload[payload_keys.IMAGE_EMBEDDING_IDENTITY] == "img-m"
        # The document fields the "is it indexed?" reads compare.
        for key in ("doc_id", "etag", "acl_principals", payload_keys.INDEX_MODE):
            assert key in payload
        assert payload[payload_keys.EMBEDDING_IDENTITY]
        assert payload["mime_type"] == PNG
        assert "chunk_index" not in payload and "excerpt" not in payload
        env.mark.assert_not_awaited()
        env.qdrant.delete.assert_not_awaited()  # nothing stale to drop

    async def test_failed_text_processor_still_indexes_the_image(self, env, mocker):
        """OCR finding no words in a photo must not cost it its image point."""
        mocker.patch.object(get_registry(), "find_processor", return_value=object())
        mocker.patch.object(
            get_registry(),
            "process_source",
            AsyncMock(
                return_value=ProcessingResult(
                    text="",
                    metadata={"parse_failed_reason": "empty_document"},
                    processor="ocr",
                    success=False,
                )
            ),
        )
        await processor._index_document(self._task(), env.nc, env.qdrant, tier=None)

        (call,) = env.qdrant.upsert.await_args_list
        (point,) = call.kwargs["points"]
        assert point.id == payload_keys.image_point_id("7")
        env.mark.assert_not_awaited()

    async def test_skip_with_no_text_dead_letters_under_the_image_key(self, env):
        env.embed.return_value = (None, image_ingest.SKIP_REJECTED)

        assert (
            await processor._index_document(self._task(), env.nc, env.qdrant, tier=None)
            is False
        )

        env.qdrant.upsert.assert_not_awaited()
        args = env.mark.await_args.args
        assert args[4] == "image_rejected"
        assert args[3].endswith(";img=img-m;mode=hybrid")
        # The old image point of the previous content goes too.
        env.qdrant.delete.assert_awaited_once()

    async def test_keyword_retag_drops_the_image_point(self, env):
        assert (
            await processor._index_document(
                self._task("keyword"), env.nc, env.qdrant, tier=None
            )
            is False  # no text processor, no image vector: dead-lettered
        )
        env.embed.assert_not_awaited()  # keyword opts out of paid embeddings
        selector = env.qdrant.delete.await_args.kwargs["points_selector"]
        assert selector.points == [payload_keys.image_point_id("7")]

    async def test_skip_with_text_marks_the_text_points(self, env, mocker):
        env.embed.return_value = (None, image_ingest.SKIP_TOO_LARGE)
        mocker.patch.object(get_registry(), "find_processor", return_value=object())
        mocker.patch.object(
            get_registry(),
            "process_source",
            AsyncMock(
                return_value=ProcessingResult(
                    text="words in a screenshot", metadata={}, processor="ocr"
                )
            ),
        )
        mocker.patch.object(
            processor,
            "get_provider",
            lambda: SimpleNamespace(
                embed_batch_with_usage=AsyncMock(return_value=([[0.1, 0.2]], 3))
            ),
        )
        mocker.patch.object(
            processor,
            "get_bm25_service",
            AsyncMock(
                return_value=SimpleNamespace(
                    encode_batch=AsyncMock(
                        return_value=[{"indices": [1], "values": [1.0]}]
                    )
                )
            ),
        )

        await processor._index_document(self._task(), env.nc, env.qdrant, tier=None)

        (point,) = env.qdrant.upsert.await_args.kwargs["points"]
        assert point.payload["chunk_index"] == 0
        assert point.payload[payload_keys.IMAGE_SKIPPED_REASON] == "too_large"
        assert point.payload[payload_keys.IMAGE_EMBEDDING_IDENTITY] == "img-m"
        env.mark.assert_not_awaited()
