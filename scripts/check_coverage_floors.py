"""Enforce per-module coverage floors on top of the global threshold.

A single global ``fail_under`` lets a well-tested module subsidise a neglected
one: ``redact.py`` could rot from 100% to 60% and the suite would still pass on
the strength of everything else. These floors pin the modules where a coverage
gap is a security or correctness risk, so the rot has to be explicit.

Floors are a ratchet. Raising one is always welcome; lowering one should be
argued for in the pull request, not done quietly.

Usage::

    pytest --cov-report=json:coverage.json
    python scripts/check_coverage_floors.py [coverage.json]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Keyed by module path relative to ``src/aws_sso_mcp/``.
FLOORS: dict[str, float] = {
    # Last line of defence against a credential reaching a log or model context.
    "redact.py": 100.0,
    "hardening.py": 100.0,
    # The only code that calls a service API, and the IAM gate it runs behind.
    "engine/catalog.py": 100.0,
    "roles.py": 100.0,
    # Error taxonomy: a leak here puts raw SDK text in front of the user.
    "errors.py": 95.0,
    # Cache invalidation decides whether a revoked role stays usable.
    "cache/accounts.py": 95.0,
    # Credential minting and the SSO token lifecycle.
    "sso/broker.py": 92.0,
    "sso/device_auth.py": 83.0,
    # Config parsing rejects secret-like keys.
    "config.py": 86.0,
}

_PACKAGE_ROOT = "src/aws_sso_mcp/"
# Floating-point noise guard, so 99.99999 does not fail a 100.0 floor.
_TOLERANCE = 1e-6
# How far above its floor a module must sit before we suggest ratcheting it up.
_RATCHET_SLACK = 5.0


def _measured(report_path: Path) -> dict[str, float]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return {
        path.replace("\\", "/").split(_PACKAGE_ROOT, 1)[-1]: entry["summary"]["percent_covered"]
        for path, entry in report["files"].items()
    }


def main(argv: list[str]) -> int:
    report_path = Path(argv[1]) if len(argv) > 1 else Path("coverage.json")
    if not report_path.is_file():
        print(
            f"error: {report_path} not found.\n       Run: pytest --cov-report=json:{report_path}",
            file=sys.stderr,
        )
        return 2

    measured = _measured(report_path)
    failures: list[str] = []
    ratchets: list[str] = []

    for module, floor in sorted(FLOORS.items()):
        actual = measured.get(module)
        if actual is None:
            failures.append(f"  {module}: not present in the report (renamed or deleted?)")
        elif actual + _TOLERANCE < floor:
            failures.append(f"  {module}: {actual:.1f}% < floor {floor:.1f}%")
        elif actual >= floor + _RATCHET_SLACK:
            ratchets.append(f"  {module}: {actual:.1f}% (floor {floor:.1f}%)")

    if ratchets:
        print("Coverage floors that could be ratcheted up:")
        print("\n".join(ratchets))

    if failures:
        print(f"\nFAIL: {len(failures)} module(s) below their coverage floor:", file=sys.stderr)
        print("\n".join(failures), file=sys.stderr)
        return 1

    print(f"OK: all {len(FLOORS)} per-module coverage floors met.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
