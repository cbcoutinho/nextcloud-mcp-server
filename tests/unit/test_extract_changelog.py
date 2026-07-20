"""Tests for ``scripts/extract-changelog.sh``.

The script produces the body of every GitHub Release (see the ``github-release``
job in ``.github/workflows/release.yml``), so a silent regression here ships
empty or wrong release notes. It runs on the release critical path but has no
other coverage, hence these subprocess tests.
"""

import subprocess
import tomllib
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent.parent / "scripts" / "extract-changelog.sh"

CHANGELOG = """\
# Changelog - MCP Server

Preamble that must never appear in release notes.

## v0.142.0 (2026-07-19)

### Feat

- **api**: remove pdf-preview endpoint

## v0.14.0 (2025-10-15)

### Fix

- an older, unrelated release

## v0.2.0 (2025-01-01)

### Feat

- the first one
"""


def run_extract(tag: str, changelog: Path) -> subprocess.CompletedProcess[str]:
    """Invoke the script for ``tag`` against a specific CHANGELOG file."""
    return subprocess.run(
        [str(SCRIPT), tag, str(changelog)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def changelog(tmp_path: Path) -> Path:
    path = tmp_path / "CHANGELOG.md"
    path.write_text(CHANGELOG, encoding="utf-8")
    return path


def test_extracts_only_the_requested_section(changelog: Path):
    """The section runs from its own heading to the next one, exclusive."""
    result = run_extract("v0.142.0", changelog)

    assert result.returncode == 0
    assert result.stdout == "### Feat\n\n- **api**: remove pdf-preview endpoint\n"


def test_does_not_match_a_longer_version_prefix(changelog: Path):
    """``v0.14.0`` must not pick up the section for ``v0.142.0``.

    The heading for a shorter version is a prefix of the longer one, so a
    prefix-only match would hand a release the wrong notes entirely. Pinning
    the behaviour here keeps that true regardless of how matching is
    implemented.
    """
    result = run_extract("v0.14.0", changelog)

    assert result.returncode == 0
    assert result.stdout == "### Fix\n\n- an older, unrelated release\n"


def test_last_section_stops_at_end_of_file(changelog: Path):
    result = run_extract("v0.2.0", changelog)

    assert result.returncode == 0
    assert result.stdout == "### Feat\n\n- the first one\n"


def test_excludes_the_preamble(changelog: Path):
    """Content above the first version heading is never part of a release."""
    for tag in ("v0.142.0", "v0.14.0", "v0.2.0"):
        assert "Preamble" not in run_extract(tag, changelog).stdout


def test_unknown_tag_fails_loudly(changelog: Path):
    """A missing section must fail the release job, not publish empty notes."""
    result = run_extract("v9.9.9", changelog)

    assert result.returncode != 0
    assert not result.stdout
    assert "no CHANGELOG section found for v9.9.9" in result.stderr


def test_missing_changelog_fails_loudly(tmp_path: Path):
    result = run_extract("v0.142.0", tmp_path / "nope.md")

    assert result.returncode != 0
    assert "not found" in result.stderr


def test_real_changelog_has_notes_for_the_current_version():
    """The shipped CHANGELOG must yield notes for the version being released."""
    repo_root = SCRIPT.parent.parent
    pyproject = tomllib.loads(
        (repo_root / "pyproject.toml").read_text(encoding="utf-8")
    )
    version = pyproject["project"]["version"]

    result = run_extract(f"v{version}", repo_root / "CHANGELOG.md")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()
