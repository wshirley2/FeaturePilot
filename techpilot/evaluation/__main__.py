"""Run a deterministic TechPilot Runtime Replay suite from the command line."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .cases import (
    CORE_V0_SUITE,
    RUNNER_VALIDATION_SUITE,
    build_core_v0_cases,
    build_runner_validation_cases,
)
from .contracts import BaselineReference
from .runner import ReplayRunner


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run offline TechPilot Runtime Replay cases.")
    parser.add_argument(
        "--suite",
        default=CORE_V0_SUITE,
        choices=[CORE_V0_SUITE, RUNNER_VALIDATION_SUITE],
    )
    parser.add_argument("--output", type=Path, help="Write the structured result manifest to this JSON path.")
    parser.add_argument(
        "--baseline-v0",
        action="store_true",
        help="Require a clean, fully passing core-v0 run before writing its formal baseline.",
    )
    parser.add_argument("--summary", action="store_true", help="Print only the suite result and case-set digest.")
    parser.add_argument(
        "--compare-baseline",
        type=Path,
        help="Compare only against a baseline report with the identical suite, track, and case-set digest.",
    )
    args = parser.parse_args(argv)
    cases = build_core_v0_cases() if args.suite == CORE_V0_SUITE else build_runner_validation_cases()
    report = ReplayRunner().run(cases)
    payload = report.to_dict()
    comparison = None
    if args.compare_baseline is not None:
        try:
            comparison = BaselineReference.from_path(args.compare_baseline).compare(report)
        except (OSError, ValueError) as error:
            parser.error(str(error))
        payload["comparison"] = comparison.to_dict()
    if args.baseline_v0:
        if args.output is None:
            parser.error("--baseline-v0 requires --output")
        try:
            ReplayRunner.write_baseline(report, args.output)
        except ValueError as error:
            parser.error(str(error))
    elif args.output is not None:
        ReplayRunner.write_report(report, args.output)
    if args.summary:
        print(f"{report.suite}: {report.passed}/{report.total} passed; case_set_digest={report.case_set_digest}")
        if comparison is not None:
            status = "comparable" if comparison.comparable else f"not-comparable:{comparison.reason}"
            print(f"baseline comparison: {status}")
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    if comparison is not None and not comparison.comparable:
        return 2
    return 0 if report.passed == report.total else 1


if __name__ == "__main__":
    raise SystemExit(main())
