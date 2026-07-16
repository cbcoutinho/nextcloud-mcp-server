"""Unit tests for per-tier worker concurrency resolution.

``cli._resolve_worker_concurrency`` picks the procrastinate worker concurrency:
explicit ``--concurrency`` (the chart's per-tier arg) > per-tier setting override
(fast/structured) > global ``vector_sync_processor_workers``.
"""

from __future__ import annotations

import pytest

from nextcloud_mcp_server.cli import _resolve_worker_concurrency

pytestmark = pytest.mark.unit


def _resolve(cli=None, tier=None, *, fast=None, structured=None, default=3):
    return _resolve_worker_concurrency(
        cli, tier, fast=fast, structured=structured, default=default
    )


def test_cli_flag_wins_over_everything():
    assert _resolve(cli=9, tier="fast", fast=4, default=3) == 9


def test_fast_tier_override_used_when_no_flag():
    assert _resolve(tier="fast", fast=4, structured=2, default=3) == 4


def test_structured_tier_override_used():
    assert _resolve(tier="structured", fast=4, structured=2, default=3) == 2


def test_falls_back_to_global_default():
    assert _resolve(tier="fast", fast=None, default=3) == 3  # no fast override
    assert _resolve(tier="ocr", fast=4, structured=2, default=3) == 3  # ocr not exposed
    assert _resolve(tier=None, fast=4, default=3) == 3  # all-tiers worker


def test_zero_override_never_yields_zero():
    # A bogus 0 must fall through, not run the worker with concurrency=0.
    assert _resolve(tier="fast", fast=0, default=3) == 3
    assert _resolve(cli=0, tier="fast", fast=4, default=3) == 4
