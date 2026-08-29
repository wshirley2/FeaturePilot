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
from .holdout import (
    HoldoutFormatError,
    default_holdout_report_path,
    holdout_case_set_metadata,
    inspect_holdout_case_schema,
    run_holdout,
    write_holdout_summary,
)
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
    parser.add_argument(
        "--holdout-root",
        type=Path,
        help="Run an external holdout-v0 directory and print/write only a redacted summary.",
    )
    parser.add_argument(
        "--holdout-case-set-metadata",
        type=Path,
        help="Print only the count and digest needed to complete an external holdout manifest.",
    )
    parser.add_argument(
        "--holdout-case-schema",
        type=Path,
        help="Print only JSONL case count and field names, never private case values.",
    )
    args = parser.parse_args(argv)
    if args.holdout_case_schema is not None:
        if args.holdout_root is not None or args.holdout_case_set_metadata is not None:
            parser.error("--holdout-case-schema cannot be combined with another holdout option")
        try:
            schema = inspect_holdout_case_schema(args.holdout_case_schema)
        except HoldoutFormatError as error:
            parser.error(str(error))
        print(f"case_count: {schema.case_count}")
        print("case_fields: " + ", ".join(schema.fields))
        return 0
    if args.holdout_case_set_metadata is not None:
        if args.holdout_root is not None or args.baseline_v0 or args.compare_baseline is not None:
            parser.error("--holdout-case-set-metadata cannot be combined with holdout run or baseline options")
        try:
            count, digest = holdout_case_set_metadata(args.holdout_case_set_metadata)
        except HoldoutFormatError as error:
            parser.error(str(error))
        print(f"case_count: {count}")
        print(f"case_set_digest: {digest}")
        return 0
    if args.holdout_root is not None:
        if args.baseline_v0 or args.compare_baseline is not None:
            parser.error("--holdout-root cannot be combined with baseline options")
        try:
            summary = run_holdout(args.holdout_root)
        except HoldoutFormatError as error:
            parser.error(str(error))
        output = args.output or default_holdout_report_path(args.holdout_root)
        write_holdout_summary(summary, output)
        print(
            f"{summary.suite}: {summary.passed}/{summary.total} passed; "
            f"case_set_digest={summary.case_set_digest}"
        )
        print(f"categories: {json.dumps(summary.categories, ensure_ascii=False, sort_keys=True)}")
        print("failed_case_ids: " + (", ".join(summary.failed_case_ids) if summary.failed_case_ids else "none"))
        print(f"summary: {output}")
        return 0 if summary.passed == summary.total else 1
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
