#!/usr/bin/env python3
"""License compliance checker for nextcloud-mcp-server.

Walks every installed Python package, classifies each license against the
policy in `.licenses/policy.toml`, and exits non-zero if a package falls
outside policy. Designed to run in CI on every PR.

Usage:
    uv run scripts/check-licenses.py                # human report
    uv run scripts/check-licenses.py --json         # machine output
    uv run scripts/check-licenses.py --markdown FILE  # write GH summary
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

POLICY_PATH = Path(__file__).resolve().parent.parent / ".licenses" / "policy.toml"


@dataclass
class Policy:
    allowed: set[str]
    denied: set[str]
    exceptions: dict[str, dict]
    overrides: dict[str, dict]

    @classmethod
    def load(cls, path: Path) -> Policy:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
        return cls(
            allowed={_norm(s) for s in raw.get("allowed", {}).get("licenses", [])},
            denied={_norm(s) for s in raw.get("denied", {}).get("licenses", [])},
            exceptions={k.lower(): v for k, v in raw.get("exceptions", {}).items()},
            overrides={k.lower(): v for k, v in raw.get("overrides", {}).items()},
        )


@dataclass
class Verdict:
    name: str
    version: str
    declared: str
    effective: str
    status: str  # allowed | denied | exception | override | unknown
    reason: str = ""
    detail: dict = field(default_factory=dict)


def _norm(s: str) -> str:
    """Lower-case, strip whitespace — used for license-string comparison."""
    return re.sub(r"\s+", " ", s.strip().lower())


# Use uppercase-only OR/AND to avoid matching "or later" inside legacy
# classifier names like "GNU Lesser General Public License v3 or later".
_OR_RE = re.compile(r"\s+OR\s+|\s*;\s*")
_AND_RE = re.compile(r"\s+AND\s+")


def _first_line(license_str: str) -> str:
    """Return the first non-empty line.

    Some packages (e.g. pythonvCard4) embed the entire LICENSE file body
    into their `License` metadata field. The license *name* is normally on
    line 1 (`MIT License`); subsequent lines are copyright/conditions.
    """
    for line in license_str.splitlines():
        if line.strip():
            return line.strip()
    return license_str.strip()


def _arms(license_str: str) -> tuple[list[str], str]:
    """Split a composite SPDX/legacy license expression into arms.

    Returns (arms, mode) where mode is "or" or "and". `;` is treated as OR
    (this matches pip-licenses' classifier-joining behaviour: a package with
    multiple `License ::` classifiers gets them joined by `; `, and the
    package is licensed under any of them).
    """
    cleaned = _first_line(license_str)
    if _AND_RE.search(cleaned) and not _OR_RE.search(cleaned):
        return [a.strip() for a in _AND_RE.split(cleaned) if a.strip()], "and"
    return [a.strip() for a in _OR_RE.split(cleaned) if a.strip()], "or"


def classify(pkg: dict, policy: Policy) -> Verdict:
    name = pkg["Name"]
    declared = pkg.get("License", "UNKNOWN") or "UNKNOWN"

    # 1. Per-package metadata override (mis-classified upstream).
    if (ov := policy.overrides.get(name.lower())) is not None:
        effective = ov["actual"]
        status, reason = _check_license(effective, policy)
        return Verdict(
            name,
            pkg["Version"],
            declared,
            effective,
            "override" if status == "allowed" else status,
            f"override: {ov.get('source', 'metadata corrected')}",
            detail=ov,
        )

    # 2. Per-package exception (e.g. AGPL/dual-licensed deps we accept).
    if (ex := policy.exceptions.get(name.lower())) is not None:
        return Verdict(
            name,
            pkg["Version"],
            declared,
            ex.get("license", declared),
            "exception",
            ex.get("rationale", "explicit exception").strip().splitlines()[0],
            detail=ex,
        )

    # 3. Match declared license against policy.
    status, reason = _check_license(declared, policy)
    return Verdict(name, pkg["Version"], declared, declared, status, reason)


def _check_license(license_str: str, policy: Policy) -> tuple[str, str]:
    arms, mode = _arms(license_str)
    if not arms:
        return "unknown", "empty license string"

    norm_arms = [_norm(a) for a in arms]

    if mode == "and":
        # Every arm must be allowed and none may be denied.
        if any(a in policy.denied for a in norm_arms):
            bad = next(a for a in norm_arms if a in policy.denied)
            return "denied", f"AND-combined with denied license: {bad!r}"
        bad = [a for a, n in zip(arms, norm_arms) if n not in policy.allowed]
        if bad:
            return "unknown", f"AND-arm not in allowlist: {bad}"
        return "allowed", "all AND-arms allowed"

    # OR: any allowed arm satisfies the policy — denied arms are tolerated
    # because the user can elect to receive the package under the allowed arm.
    good = [a for a, n in zip(arms, norm_arms) if n in policy.allowed]
    if good:
        return "allowed", f"OR-arm allowed: {good[0]!r}"
    if all(n in policy.denied for n in norm_arms):
        return "denied", f"all OR-arms denied: {arms}"
    return "unknown", f"no OR-arm in allowlist: {arms}"


def collect_packages(self_pkg: str | None) -> list[dict]:
    out = subprocess.check_output(
        ["uv", "run", "--frozen", "pip-licenses", "--format=json", "--with-urls"],
        text=True,
    )
    packages = json.loads(out)
    if self_pkg:
        packages = [p for p in packages if p["Name"].lower() != self_pkg.lower()]
    return packages


def render_markdown(verdicts: list[Verdict]) -> str:
    bad = [v for v in verdicts if v.status in ("denied", "unknown")]
    review = [v for v in verdicts if v.status == "exception"]
    overrides = [v for v in verdicts if v.status == "override"]
    ok = [v for v in verdicts if v.status == "allowed"]

    lines: list[str] = []
    lines.append("# License compliance report\n")
    lines.append(
        f"- Total packages scanned: **{len(verdicts)}**\n"
        f"- Allowed: **{len(ok)}**\n"
        f"- Allowed via metadata override: **{len(overrides)}**\n"
        f"- Allowed via per-package exception: **{len(review)}**\n"
        f"- **Failures: {len(bad)}**\n"
    )

    if bad:
        lines.append("\n## ❌ Failures (blocking)\n")
        lines.append("| Package | Version | License | Reason |")
        lines.append("|---|---|---|---|")
        for v in bad:
            lines.append(f"| `{v.name}` | {v.version} | {v.declared} | {v.reason} |")

    if review:
        lines.append("\n## ⚠️ Per-package exceptions (review on version bump)\n")
        lines.append("| Package | Version | License | Allowed for | Review for |")
        lines.append("|---|---|---|---|---|")
        for v in review:
            d = v.detail
            lines.append(
                f"| `{v.name}` | {v.version} | {v.effective} | "
                f"{', '.join(d.get('allowed_for', []) or ['—'])} | "
                f"{', '.join(d.get('review_required_for', []) or ['—'])} |"
            )

    if overrides:
        lines.append("\n## 🔧 Metadata overrides\n")
        lines.append("| Package | Version | Declared | Actual | Source |")
        lines.append("|---|---|---|---|---|")
        for v in overrides:
            d = v.detail
            lines.append(
                f"| `{v.name}` | {v.version} | {v.declared} | "
                f"{d.get('actual', v.effective)} | {d.get('source', '')} |"
            )

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit JSON to stdout")
    parser.add_argument(
        "--markdown",
        type=Path,
        help="Write a Markdown report to the given path (e.g. $GITHUB_STEP_SUMMARY)",
    )
    parser.add_argument(
        "--self-name",
        default="nextcloud-mcp-server",
        help="Package name to skip (the project itself)",
    )
    args = parser.parse_args()

    policy = Policy.load(POLICY_PATH)
    packages = collect_packages(args.self_name)
    verdicts = [classify(p, policy) for p in packages]
    verdicts.sort(key=lambda v: (v.status, v.name.lower()))

    if args.markdown:
        args.markdown.write_text(render_markdown(verdicts))

    if args.json:
        json.dump(
            [v.__dict__ for v in verdicts],
            sys.stdout,
            indent=2,
            default=str,
        )
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render_markdown(verdicts))

    failures = [v for v in verdicts if v.status in ("denied", "unknown")]
    if failures:
        sys.stderr.write(
            f"\n{len(failures)} package(s) violate license policy "
            f"(see report above). Update .licenses/policy.toml or replace the dep.\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
