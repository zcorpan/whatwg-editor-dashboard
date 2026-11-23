#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from editor_dashboard.build import build_site
from editor_dashboard.config import load_config
from editor_dashboard.github import fetch_with_snapshot, load_fixture
from editor_dashboard.models import parse_datetime
from editor_dashboard.state import full_fetch_reason, load_snapshot, save_snapshot


def _parse_now(value: str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    parsed = parse_datetime(value)
    if parsed is None:
        raise argparse.ArgumentTypeError("--now must be an ISO 8601 timestamp")
    return parsed


def build_command(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    now = _parse_now(args.now)

    if args.fixture:
        if args.state or args.full:
            raise SystemExit("--state and --full apply to GitHub fetches, not to --fixture")
        repository_data = load_fixture(args.fixture)
    else:
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            raise SystemExit("GITHUB_TOKEN is required unless --fixture is used")
        previous = None
        if args.state:
            try:
                previous = load_snapshot(args.state)
            except (OSError, ValueError, KeyError) as error:
                # A corrupt snapshot costs one full fetch, not the build.
                logging.warning("Ignoring unreadable snapshot in %s: %s", args.state, error)
        if args.full:
            reason = "--full was given"
        elif not args.state:
            reason = "no state directory"
        else:
            reason = full_fetch_reason(previous, config, now=now)
        repository_data, snapshot = fetch_with_snapshot(
            config, token, now=now, previous=previous, full_reason=reason
        )
        if args.state:
            save_snapshot(args.state, snapshot)
        metadata = repository_data.metadata
        print(
            f"Fetched {metadata.fetch_mode}"
            + (f" ({metadata.full_fetch_reason})" if metadata.full_fetch_reason else "")
            + f": {metadata.query_count} GraphQL queries, cost {metadata.total_cost}."
        )

    payload = build_site(config, repository_data, output_dir=args.output, now=now)
    current = payload["metrics"]["repository"]["current"]
    print(
        f"Built {args.output}: {current['open_prs']} open PRs, "
        f"{current['active_now']} active now, "
        f"{current['direct_requests']} direct requests, "
        f"{current['waiting_on_editor']} waiting on an editor."
    )
    return 0


def validate_command(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    print(
        f"Configuration is valid for {config.repository.slug}; "
        f"editors={len(config.editors)} ({', '.join('@' + login for login in sorted(config.editors))})."
    )
    return 0


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a public static WHATWG editor dashboard with browser-local workflow state."
    )
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="Fetch/analyse PRs and build the static site")
    build.add_argument("--config", default="dashboard.yml")
    build.add_argument("--output", default="site")
    build.add_argument("--fixture", help="Build from a local fixture instead of GitHub")
    build.add_argument("--now", help="Override current time (ISO 8601), useful for deterministic fixtures")
    build.add_argument(
        "--state",
        help="Directory holding the previous build's raw snapshot; fetch only what changed since it, then update it",
    )
    build.add_argument("--full", action="store_true", help="Fetch everything even if --state has a usable snapshot")
    build.set_defaults(handler=build_command)

    validate = subparsers.add_parser("validate", help="Validate dashboard.yml")
    validate.add_argument("--config", default="dashboard.yml")
    validate.set_defaults(handler=validate_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = create_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return int(args.handler(args))
    except (OSError, ValueError) as error:
        logging.error("%s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
