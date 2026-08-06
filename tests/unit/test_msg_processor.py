"""Outlook messages keep their envelope, which is most of what gets searched."""

import io
import struct
from datetime import datetime, timezone

import pytest

from nextcloud_mcp_server.document_processors._msg_reader import (
    MsgReadError,
    read_msg,
)
from nextcloud_mcp_server.document_processors.base import ProcessorError
from nextcloud_mcp_server.document_processors.msg import (
    ATTACHMENT_NAMES_KEY,
    MsgProcessor,
)
from tests.support.cfb_writer import write_cfb

pytestmark = pytest.mark.unit

MSG_MIME = "application/vnd.ms-outlook"

# 2026-02-07 10:02:02 UTC as a FILETIME (100ns ticks since 1601-01-01).
SUBMIT_FILETIME = int(
    (
        datetime(2026, 2, 7, 10, 2, 2, tzinfo=timezone.utc)
        - datetime(1601, 1, 1, tzinfo=timezone.utc)
    ).total_seconds()
    * 10_000_000
)


def _build_msg(
    *,
    subject: str | None = "Re: Something for the weekend",
    sender: str | None = "john@example.com",
    to: str | None = "paul@example.org",
    body: str | None = "Hi Paul,\n\nThe partners will catch up on Monday.",
    unicode_body: bool = False,
    attachments: tuple[str, ...] = (),
    submit_time: int | None = SUBMIT_FILETIME,
    codepage: int | None = None,
) -> bytes:
    """A minimal but genuine OLE2 .msg carrying the streams we read."""
    streams: dict[str, bytes] = {}

    def put(tag: str, value: str | None, *, as_unicode: bool, prefix: str = ""):
        if value is None:
            return
        suffix = "001F" if as_unicode else "001E"
        raw = value.encode("utf-16-le" if as_unicode else "cp1252")
        streams[f"{prefix}__substg1.0_{tag}{suffix}"] = raw

    put("0037", subject, as_unicode=False)
    put("5D01", sender, as_unicode=False)
    put("0E04", to, as_unicode=False)
    put("1000", body, as_unicode=unicode_body)
    for i, name in enumerate(attachments):
        put("3707", name, as_unicode=False, prefix=f"__attach_version1.0_#{i:08X}/")

    # __properties_version1.0: 32-byte header, then 16-byte entries of
    # (tag<<16|type, flags, 8-byte value).
    props = bytearray(b"\x00" * 32)
    if submit_time is not None:
        props += struct.pack("<IIQ", 0x0039 << 16 | 0x0040, 0, submit_time)
    if codepage is not None:
        props += struct.pack("<II", 0x3FDE << 16 | 0x0003, 0)
        props += struct.pack("<II", codepage, 0)
    streams["__properties_version1.0"] = bytes(props)

    return write_cfb(streams)


@pytest.fixture
def sample_msg():
    return _build_msg()


class TestReader:
    def test_a_codepage_body_is_read_not_skipped(self, sample_msg):
        """The 001E variant is what a real message carried and markitdown missed."""
        message = read_msg(io.BytesIO(sample_msg))

        assert "The partners will catch up on Monday." in message["body"]

    def test_a_unicode_body_is_read_too(self):
        content = _build_msg(body="Unicode body — with a dash", unicode_body=True)

        assert "Unicode body — with a dash" in read_msg(io.BytesIO(content))["body"]

    def test_the_submit_time_is_decoded_from_the_properties_stream(self, sample_msg):
        assert read_msg(io.BytesIO(sample_msg))["date"] == datetime(
            2026, 2, 7, 10, 2, 2, tzinfo=timezone.utc
        )

    def test_a_zero_or_absent_timestamp_yields_no_date(self):
        assert read_msg(io.BytesIO(_build_msg(submit_time=0)))["date"] is None
        assert read_msg(io.BytesIO(_build_msg(submit_time=None)))["date"] is None

    def test_attachment_names_come_back_in_storage_order(self):
        content = _build_msg(attachments=("contract.docx", "image001.png"))

        assert read_msg(io.BytesIO(content))["attachments"] == [
            "contract.docx",
            "image001.png",
        ]

    def test_a_non_ole_container_is_rejected_clearly(self):
        with pytest.raises(MsgReadError, match="not an OLE2"):
            read_msg(io.BytesIO(b"this is not a compound file"))

    def test_an_item_without_sender_or_body_still_reads(self):
        """.msg also stores contacts and calendar items, which have neither."""
        message = read_msg(io.BytesIO(_build_msg(sender=None, body=None, to=None)))

        assert message["sender"] is None
        assert message["body"] is None
        assert message["subject"] == "Re: Something for the weekend"


class TestProcessor:
    async def test_headers_and_body_are_both_indexed(self, sample_msg):
        result = await MsgProcessor().process(sample_msg, MSG_MIME, "m.msg")

        assert result.text.startswith("# Re: Something for the weekend")
        assert "**From:** john@example.com" in result.text
        assert "**Date:** 2026-02-07T10:02:02+00:00" in result.text
        assert "The partners will catch up on Monday." in result.text
        # An absent header must not leave a dangling label.
        assert "**Cc:**" not in result.text

    async def test_attachment_names_are_recorded_and_indexed(self):
        content = _build_msg(attachments=("contract.docx",))

        result = await MsgProcessor().process(content, MSG_MIME, "m.msg")

        assert result.metadata[ATTACHMENT_NAMES_KEY] == ["contract.docx"]
        assert result.metadata["attachment_count"] == 1
        assert "contract.docx" in result.text

    async def test_a_message_with_no_subject_still_produces_a_document(self):
        """markitdown returns 27 bytes here; anything real must not."""
        content = _build_msg(subject=None)

        result = await MsgProcessor().process(content, MSG_MIME, "m.msg")

        assert "(no subject)" in result.text
        assert "The partners will catch up on Monday." in result.text

    async def test_parse_failure_becomes_a_processor_error(self):
        with pytest.raises(ProcessorError, match="Outlook message parse failed"):
            await MsgProcessor().process(b"garbage", MSG_MIME, "m.msg")
