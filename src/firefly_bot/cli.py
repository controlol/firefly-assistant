"""Command-line entrypoint: `firefly-bot run` and `firefly-bot import`."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from firefly_bot.banking.camt import parse_camt053
from firefly_bot.banking.importer import import_statement
from firefly_bot.config import BankSettings, FireflySettings, load_settings
from firefly_bot.firefly.client import FireflyClient
from firefly_bot.ingest.source import AttachmentSource, FolderAttachmentSource
from firefly_bot.pipeline import run

log = logging.getLogger("firefly_bot")


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true")
    common.add_argument(
        "--dry-run", action="store_true", help="Plan and report, but write nothing to Firefly."
    )

    parser = argparse.ArgumentParser(prog="firefly-bot")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", parents=[common], help="Ingest documents, match, attach.")
    run_p.add_argument("--source", choices=["imap", "folder"], default="imap")
    run_p.add_argument("--folder", default="samples/invoices")

    import_p = sub.add_parser(
        "import", parents=[common], help="Import a CAMT.053 bank statement into Firefly."
    )
    import_p.add_argument("--camt", required=True, help="Path to the CAMT.053 .xml file.")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.command == "run":
        return _run(args)
    if args.command == "import":
        return _import(Path(args.camt), dry_run=args.dry_run)
    return 1


def _run(args: argparse.Namespace) -> int:
    settings = load_settings()
    if args.dry_run:
        log.info("DRY RUN — no writes to Firefly.")
    source: AttachmentSource | None = (
        FolderAttachmentSource(args.folder) if args.source == "folder" else None
    )
    report_path = run(settings, source=source, dry_run=args.dry_run)
    print(f"Report: {report_path}")
    return 0


def _import(camt_path: Path, *, dry_run: bool) -> int:
    statement = parse_camt053(camt_path)
    bank = BankSettings()
    log.info(
        "Parsed %d entries for %s (%s)",
        len(statement.transactions),
        statement.account_iban,
        statement.currency,
    )
    with FireflyClient(FireflySettings()) as client:
        summary = import_statement(
            statement,
            client,
            owner_name=bank.owner_name,
            own_ibans=frozenset(bank.own_ibans),
            account_name=bank.account_name,
            dry_run=dry_run,
        )
    prefix = "(dry-run) " if dry_run else ""
    print(
        f"{prefix}Import: parsed {summary.total}, created {summary.created}, "
        f"duplicates {summary.duplicates}, errors {summary.errors}, transfers {summary.transfers}"
    )
    return 1 if summary.errors else 0


if __name__ == "__main__":
    sys.exit(main())
