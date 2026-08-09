"""Command-line entry point for FeaturePilot."""

import argparse
import json
from pathlib import Path

from . import __version__
from .repository import RepositoryProfiler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="featurepilot",
        description="A repo-aware coding agent workspace.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("status", help="Show the initial application status.")
    profile_parser = subparsers.add_parser(
        "profile",
        help="Analyze a local repository and print its profile as JSON.",
    )
    profile_parser.add_argument(
        "repository",
        type=Path,
        help="Path to the local repository to analyze.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "status":
        print("FeaturePilot skeleton is ready.")
        return 0
    if args.command == "profile":
        if not args.repository.is_dir():
            parser.error(f"Repository directory does not exist: {args.repository}")
        profile = RepositoryProfiler().profile(args.repository)
        print(json.dumps(profile.to_dict(), ensure_ascii=False, indent=2))
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
