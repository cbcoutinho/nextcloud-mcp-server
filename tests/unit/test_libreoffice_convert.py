"""LibreOffice invocation: private profile, real output check, clear failures."""

import pathlib

import pytest

from nextcloud_mcp_server.document_processors import _libreoffice

pytestmark = pytest.mark.unit


class _Completed:
    def __init__(self, returncode=0, stderr=b""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = b""


def _patch_run(mocker, *, returncode=0, stderr=b"", writes: str | None = "out.pdf"):
    """Fake soffice: optionally write an output file into --outdir."""

    async def fake_run(argv, **kwargs):
        if writes is not None:
            outdir = pathlib.Path(argv[argv.index("--outdir") + 1])
            (outdir / writes).write_bytes(b"%PDF-1.7 rendered")
        fake_run.argv = argv
        return _Completed(returncode, stderr)

    mocker.patch.object(_libreoffice.anyio, "run_process", fake_run)
    mocker.patch.object(_libreoffice, "SOFFICE_BIN", "/usr/bin/soffice")
    return fake_run


async def test_each_invocation_gets_a_private_profile(mocker):
    """Concurrent soffice runs sharing one profile silently produce nothing."""
    run = _patch_run(mocker)

    await _libreoffice.convert(b"x", "a.docx", "pdf")

    profile_args = [a for a in run.argv if a.startswith("-env:UserInstallation=")]
    assert len(profile_args) == 1
    assert profile_args[0].startswith("-env:UserInstallation=file://")


async def test_output_bytes_are_returned(mocker):
    _patch_run(mocker)

    assert await _libreoffice.convert(b"x", "a.docx", "pdf") == b"%PDF-1.7 rendered"


async def test_success_exit_with_no_output_file_is_still_a_failure(mocker):
    """soffice exits 0 on an unreadable input while writing nothing at all."""
    _patch_run(mocker, writes=None)

    with pytest.raises(_libreoffice.LibreOfficeError, match="produced no pdf output"):
        await _libreoffice.convert(b"x", "a.docx", "pdf")


async def test_nonzero_exit_reports_stderr(mocker):
    _patch_run(mocker, returncode=1, stderr=b"source file could not be loaded")

    with pytest.raises(_libreoffice.LibreOfficeError, match="could not be loaded"):
        await _libreoffice.convert(b"x", "a.docx", "pdf")


async def test_extensionless_name_is_rejected(mocker):
    """Without a suffix LibreOffice picks the wrong import filter silently."""
    mocker.patch.object(_libreoffice, "SOFFICE_BIN", "/usr/bin/soffice")

    with pytest.raises(_libreoffice.LibreOfficeError, match="no extension"):
        await _libreoffice.convert(b"x", "document", "pdf")


async def test_missing_libreoffice_is_reported_not_crashed(mocker):
    mocker.patch.object(_libreoffice, "SOFFICE_BIN", None)

    with pytest.raises(_libreoffice.LibreOfficeError, match="not installed"):
        await _libreoffice.convert(b"x", "a.docx", "pdf")


async def test_conversions_are_bounded_by_the_parse_limiter(mocker):
    """Unbounded, a folder of .doc files would start one soffice per task.

    Counts processes actually in flight rather than inspecting the limiter
    afterwards: the limiter always reads as empty once the work is done, so a
    post-hoc assertion would pass even with no bound at all.
    """
    import anyio as anyio_module

    in_flight = 0
    peak = 0

    async def fake_run(argv, **kwargs):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        # Yield so a sibling task can start if nothing is holding it back.
        await anyio_module.sleep(0.01)
        outdir = pathlib.Path(argv[argv.index("--outdir") + 1])
        (outdir / "out.pdf").write_bytes(b"%PDF-1.7")
        in_flight -= 1
        return _Completed()

    mocker.patch.object(_libreoffice.anyio, "run_process", fake_run)
    mocker.patch.object(_libreoffice, "SOFFICE_BIN", "/usr/bin/soffice")
    mocker.patch.object(
        _libreoffice,
        "get_settings",
        lambda: mocker.Mock(document_parse_process_slots=2),
    )
    limiter = anyio_module.CapacityLimiter(2)
    mocker.patch.object(_libreoffice, "parse_process_limiter", lambda slots: limiter)

    async with anyio_module.create_task_group() as tg:
        for _ in range(6):
            tg.start_soon(_libreoffice.convert, b"x", "a.docx", "pdf")

    assert peak == 2, f"expected at most 2 concurrent soffice processes, saw {peak}"


async def test_the_temp_directory_is_cleaned_up(mocker):
    """A rendition holds the whole document twice; leaking it fills the disk."""
    run = _patch_run(mocker)

    await _libreoffice.convert(b"x", "a.docx", "pdf")

    src = pathlib.Path(run.argv[-1])
    assert not src.parent.exists()
