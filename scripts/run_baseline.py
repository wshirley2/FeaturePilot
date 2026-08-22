"""Run the deterministic FeaturePilot E2-lite benchmark suite."""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "benchmarks" / "baseline_cases.json"
DEFAULT_OUTPUT = ROOT / ".tmp" / "baseline" / "latest.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run FeaturePilot's deterministic E2-lite baseline.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST, help="Baseline case manifest.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="JSON result path.")
    parser.add_argument("--case", action="append", dest="case_ids", help="Run only one case id; repeatable.")
    parser.add_argument("--list", action="store_true", help="List cases without running them.")
    return parser


def _load_cases(path: Path) -> list[dict[str, object]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"Baseline manifest not found: {path}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"Baseline manifest is not valid JSON: {path}") from error
    cases = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(cases, list) or not cases:
        raise ValueError("Baseline manifest must contain a non-empty cases list")
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not isinstance(case.get("test"), str):
            raise TypeError("Each baseline case requires string id and test fields")
    return cases


def _tail(value: str, limit: int = 2000) -> str:
    return value[-limit:] if len(value) > limit else value


def run_case(case: dict[str, object], index: int) -> dict[str, object]:
    case_id = str(case["id"])
    test = str(case["test"])
    base_temp = ROOT / ".tmp" / "baseline" / f"{index:02d}-{case_id}"
    command = [
        sys.executable,
        "-m",
        "pytest",
        test,
        "-q",
        "-p",
        "no:cacheprovider",
        f"--basetemp={base_temp}",
    ]
    base_temp.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    result = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    duration = time.perf_counter() - started
    return {
        "id": case_id,
        "title": case.get("title"),
        "mode": case.get("mode"),
        "test": test,
        "status": "passed" if result.returncode == 0 else "failed",
        "duration_seconds": round(duration, 3),
        "expected": case.get("expected", {}),
        "output_tail": _tail((result.stdout or "") + (result.stderr or "")),
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    cases = _load_cases(args.manifest)
    if args.case_ids:
        requested = set(args.case_ids)
        cases = [case for case in cases if case["id"] in requested]
        missing = requested - {case["id"] for case in cases}
        if missing:
            raise ValueError(f"Unknown baseline case id(s): {', '.join(sorted(missing))}")
    if args.list:
        for case in cases:
            print(f"{case['id']}: {case.get('title', '')} ({case['mode']})")
        return 0

    results = []
    for index, case in enumerate(cases, start=1):
        print(f"[{index}/{len(cases)}] {case['id']}: {case.get('title', '')}")
        result = run_case(case, index)
        results.append(result)
        print(f"  {result['status']} ({result['duration_seconds']}s)")

    summary = {
        "total": len(results),
        "passed": sum(result["status"] == "passed" for result in results),
        "failed": sum(result["status"] != "passed" for result in results),
    }
    payload = {
        "schema_version": 1,
        "suite": "FeaturePilot E2-lite deterministic baseline",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "results": results,
        "summary": summary,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Result: {summary['passed']}/{summary['total']} passed → {args.output}")
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
