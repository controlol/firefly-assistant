"""Command-line entrypoint: `firefly-bot run`."""

from __future__ import annotations

import argparse
import logging
import sys

from firefly_bot.config import load_settings
from firefly_bot.pipeline import run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="firefly-bot")
    parser.add_argument("command", choices=["run"], help="Action to perform.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read, match and report, but do not attach or tag anything in Firefly.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.command == "run":
        settings = load_settings()
        if args.dry_run:
            logging.getLogger("firefly_bot").info("DRY RUN — no writes to Firefly.")
        report_path = run(settings, dry_run=args.dry_run)
        print(f"Report: {report_path}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
