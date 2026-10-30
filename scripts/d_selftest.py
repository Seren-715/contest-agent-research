"""Run the D-class public self-test against an implementation file.

Usage:
    uv run python scripts/d_selftest.py --case D-01 --implementation path/to/implementation.py

The implementation is copied verbatim into a sandbox next to the case's worker.py.
This script never calls the platform Gateway or the platform experiment client;
it only answers "does this source pass the public protocol checks?".
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.budget import Deadline  # noqa: E402
from core.config import load_settings  # noqa: E402
from d_adapter.selftest import run_selftest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True, choices=["D-01", "D-02", "D-05"])
    parser.add_argument("--implementation", required=True, type=Path)
    parser.add_argument("--materials", type=Path, default=ROOT / "vendor" / "d-class")
    parser.add_argument("--samples", type=Path, default=None)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--work-dir", type=Path, default=None)
    args = parser.parse_args()

    settings = load_settings()
    settings = dataclasses.replace(
        settings,
        d_materials_root=args.materials,
        d_samples_root=args.samples or (args.materials / "公开样例请求"),
        d_selftest_timeout=args.timeout,
    )
    work_dir = args.work_dir or (ROOT / "runs" / "selftest" / args.case)
    work_dir.mkdir(parents=True, exist_ok=True)

    report = run_selftest(
        args.case,
        args.implementation,
        settings,
        work_dir,
        Deadline(settings.case_budget),
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
