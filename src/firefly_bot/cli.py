"""Command-line entrypoint: `firefly-bot run`, `import`, and `bootstrap`."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from firefly_bot.banking.camt import parse_camt053
from firefly_bot.banking.importer import import_statement
from firefly_bot.config import Settings, load_settings
from firefly_bot.enrich.bootstrap import bootstrap_labels
from firefly_bot.enrich.corrections import capture_corrections
from firefly_bot.firefly.client import FireflyClient
from firefly_bot.ingest.source import AttachmentSource, FolderAttachmentSource
from firefly_bot.labels import JsonlLabelStore, NullLabelStore, read_labels
from firefly_bot.pipeline import run

if TYPE_CHECKING:
    from firefly_bot.enrich import Categoriser

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

    bootstrap_p = sub.add_parser(
        "bootstrap",
        parents=[common],
        help="Seed labels.jsonl from existing categorised Firefly history (read-only).",
    )
    window = bootstrap_p.add_mutually_exclusive_group()
    window.add_argument(
        "--days", type=int, default=365, help="Look back this many days (default: 365)."
    )
    window.add_argument("--since", help="Look back from this date (YYYY-MM-DD) instead of --days.")

    reconcile_p = sub.add_parser(
        "reconcile-labels",
        parents=[common],
        help="Capture human category corrections from Firefly into labels.jsonl (read-only API).",
    )
    rwindow = reconcile_p.add_mutually_exclusive_group()
    rwindow.add_argument(
        "--days", type=int, default=365, help="Search Firefly back this many days (default: 365)."
    )
    rwindow.add_argument(
        "--since", help="Search Firefly from this date (YYYY-MM-DD) instead of --days."
    )

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.command == "run":
        return _run(args)
    if args.command == "import":
        return _import(Path(args.camt), dry_run=args.dry_run)
    if args.command == "bootstrap":
        return _bootstrap(args)
    if args.command == "reconcile-labels":
        return _reconcile(args)
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
    settings = load_settings()
    bank = settings.bank
    log.info(
        "Parsed %d entries for %s (%s)",
        len(statement.transactions),
        statement.account_iban,
        statement.currency,
    )
    # Mirror the ledger: suppress label writes on dry-run, otherwise accumulate to labels.jsonl.
    label_store = NullLabelStore() if dry_run else JsonlLabelStore(settings.labels_path)
    categoriser = _build_categoriser(settings)
    with FireflyClient(settings.firefly) as client:
        summary = import_statement(
            statement,
            client,
            owner_name=bank.owner_name,
            own_ibans=frozenset(bank.own_ibans),
            account_name=bank.account_name,
            dry_run=dry_run,
            label_store=label_store,
            categoriser=categoriser,
            needs_review_tag=settings.matching.needs_review_tag,
        )
    label_store.close()
    prefix = "(dry-run) " if dry_run else ""
    print(
        f"{prefix}Import: parsed {summary.total}, created {summary.created}, "
        f"duplicates {summary.duplicates}, errors {summary.errors}, transfers {summary.transfers}"
    )
    return 1 if summary.errors else 0


def _build_categoriser(settings: Settings) -> Categoriser | None:
    """Build the Phase 2 categoriser from seeded labels, or None for MCC-only behaviour.

    Lazy by design: when enrichment is disabled or there are no seeded labels, we return None and
    never touch the embedder — so an import without enrichment never loads the e5 model. Only when
    `settings.enrich.enabled` AND `labels.jsonl` has `category`-kind examples do we import the
    enrich package, construct the embedder, and build the categoriser. The label inventory is the
    example labels plus every category the MCC map can emit (the Categoriser unions in MCC names
    itself); zero-shot can therefore place a named-but-exampleless category from day one.
    """
    if not settings.enrich.enabled:
        return None
    examples = [
        rec
        for rec in read_labels(settings.labels_path)
        if rec.kind == "category" and rec.predicted is not None
    ]
    if not examples:
        return None
    # Lazy import: only pay the enrich/embedder cost when we actually categorise.
    from firefly_bot.enrich import Categoriser as _Categoriser
    from firefly_bot.enrich import E5Embedder

    inventory = {rec.predicted for rec in examples if rec.predicted}
    return _Categoriser(
        examples,
        inventory,
        E5Embedder(model_name=settings.enrich.model_name),
        gate=settings.enrich.gate,
        knn_trust=settings.enrich.knn_trust,
    )


def _bootstrap(args: argparse.Namespace) -> int:
    """Read-only pass over existing Firefly history → seed labels.jsonl for the Phase 2 enricher."""
    settings = load_settings()
    if args.since:
        start = date.fromisoformat(args.since)
    else:
        start = (datetime.now(tz=UTC) - timedelta(days=args.days)).date()
    end = datetime.now(tz=UTC).date()

    # Prior seeds make the run idempotent — re-running never duplicates a record.
    existing = list(read_labels(settings.labels_path))

    if args.dry_run:
        log.info("DRY RUN — no writes to labels.jsonl.")
        store: JsonlLabelStore | NullLabelStore = NullLabelStore()
    else:
        store = JsonlLabelStore(settings.labels_path)

    with FireflyClient(settings.firefly) as client:
        transactions = client.list_transactions(start=start, end=end)
        summary = bootstrap_labels(transactions, store, existing=existing)
    store.close()

    prefix = "(dry-run) " if args.dry_run else ""
    print(
        f"{prefix}Bootstrap ({start} to {end}): scanned {summary.scanned}, "
        f"categorised {summary.categorised}, category records {summary.category_records}, "
        f"merchant records {summary.merchant_records}, "
        f"skipped duplicates {summary.skipped_duplicates}"
    )
    return 0


def _reconcile(args: argparse.Namespace) -> int:
    """Capture human category corrections from Firefly into labels.jsonl (read-only on Firefly).

    Loads prior labels, finds the transactions behind the still-uncorrected category predictions,
    re-reads their live state from Firefly, and appends a ``corrected`` record wherever the user
    changed what we predicted. The only thing written is the local label file.
    """
    settings = load_settings()
    if args.since:
        start = date.fromisoformat(args.since)
    else:
        start = (datetime.now(tz=UTC) - timedelta(days=args.days)).date()
    end = datetime.now(tz=UTC).date()

    prior = list(read_labels(settings.labels_path))
    wanted = {
        fid
        for rec in prior
        if rec.kind == "category" and rec.corrected is None
        and isinstance((fid := rec.features.get("firefly_id")), str)
    }
    if not wanted:
        print("Reconcile: no uncorrected category records with a firefly_id — nothing to do.")
        return 0

    store = NullLabelStore() if args.dry_run else JsonlLabelStore(settings.labels_path)
    if args.dry_run:
        log.info("DRY RUN — no writes to labels.jsonl.")
    with FireflyClient(settings.firefly) as client:
        current = {
            tx.id: tx
            for tx in client.list_transactions(start=start, end=end)
            if tx.id in wanted
        }
        summary = capture_corrections(prior, current, store)
    store.close()

    prefix = "(dry-run) " if args.dry_run else ""
    print(
        f"{prefix}Reconcile ({start} to {end}): checked {summary.checked}, "
        f"corrections {summary.corrections}, unchanged {summary.unchanged}, "
        f"unresolved {summary.unresolved}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
