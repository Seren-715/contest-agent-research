"""Local pre-submission validator.

The platform performs the official check after upload. This script mirrors the
parts we can verify offline: 11 result JSONs, schema, C/E Markdown consistency,
D figure existence, and B filenames against the local paper library.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.schemas import SCHEMAS, validate_answer  # noqa: E402

FUNCTION_BY_PREFIX = {
    "A": "Chat",
    "B": "Literature_Retrieval",
    "C": "Survey",
    "D": "Reproduction",
    "E": "Idea",
}

FIXED_CASES = {
    "B-01": "Literature_Retrieval",
    "B-02": "Literature_Retrieval",
    "B-03": "Literature_Retrieval",
    "C-01": "Survey",
    "C-03": "Survey",
    "D-01": "Reproduction",
    "D-02": "Reproduction",
    "D-05": "Reproduction",
    "E-01": "Idea",
    "E-02": "Idea",
}


def _function_for(case_id: str) -> str | None:
    return FUNCTION_BY_PREFIX.get(case_id.split("-", 1)[0])


def validate_submission(root: Path, team: str) -> list[str]:
    errors: list[str] = []
    team_dir = root / "data" / team
    if not team_dir.exists():
        return [f"missing team directory: {team_dir}"]

    cases: dict[str, str] = {}
    dispatch_path = team_dir / "dispatch.json"
    if dispatch_path.exists():
        try:
            dispatch = json.loads(dispatch_path.read_text(encoding="utf-8"))
            for item in dispatch.get("cases", []):
                case_id = item.get("case_id")
                function = item.get("function") or _function_for(str(case_id))
                if isinstance(case_id, str) and isinstance(function, str):
                    cases[case_id] = function
        except Exception as exc:
            errors.append(f"dispatch.json unreadable: {exc}")
    if not cases:
        for path in sorted(team_dir.glob("*.json")):
            if path.name == "dispatch.json":
                continue
            function = _function_for(path.stem)
            if function:
                cases[path.stem] = function
        for case_id, function in FIXED_CASES.items():
            if case_id not in cases:
                errors.append(f"{case_id}: missing fixed case")
            cases.setdefault(case_id, function)
        if not any(case_id.startswith("A-") for case_id in cases):
            errors.append("A-*: missing Chat case")

    if len(cases) != 11:
        errors.append(f"expected 11 result cases, found {len(cases)}")

    paper_root = root / "data" / "论文库"
    for case_id, function in sorted(cases.items()):
        path = team_dir / f"{case_id}.json"
        if not path.exists():
            errors.append(f"{case_id}: missing {path}")
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"{case_id}: invalid JSON: {exc}")
            continue
        if payload.get("case_id") != case_id:
            errors.append(f"{case_id}: case_id mismatch: {payload.get('case_id')!r}")
        if function not in SCHEMAS:
            errors.append(f"{case_id}: unknown function {function!r}")
            continue
        try:
            validate_answer(function, payload)
        except Exception as exc:
            errors.append(f"{case_id}: schema error: {exc}")
            continue

        if function in ("Survey", "Idea"):
            relative = payload.get("path", "")
            report = root / relative
            if not report.exists():
                errors.append(f"{case_id}: report missing: {relative}")
            elif report.read_text(encoding="utf-8") != payload.get("markdown"):
                errors.append(f"{case_id}: JSON markdown != report file")

        if function == "Reproduction":
            for figure in payload.get("figures", []):
                if not (root / figure["path"]).exists():
                    errors.append(f"{case_id}: figure missing: {figure['path']}")

        if function == "Literature_Retrieval" and paper_root.exists():
            actual = {paper.name.lower() for paper in paper_root.rglob("*") if paper.is_file()}
            for paper in payload.get("papers", []):
                if paper.get("filename", "").lower() not in actual:
                    errors.append(f"{case_id}: paper not found locally: {paper.get('filename')}")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--team", required=True)
    parser.add_argument("--root", type=Path, default=Path("/output"))
    args = parser.parse_args()
    root = args.root
    if not root.exists():
        root = Path(".")
    errors = validate_submission(root, args.team)
    if errors:
        print("INVALID")
        for error in errors:
            print("-", error)
        return 1
    print("OK: 11 result JSONs, Markdown, figures and paper filenames look valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
