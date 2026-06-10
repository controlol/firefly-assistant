"""Orchestrates a single run: ingest -> extract -> match -> act -> report.

Every I/O boundary (attachment source, OCR recogniser, Firefly ledger, report writer) is an
injected Protocol with a real default, so `run` is fully testable with fakes and `--dry-run`
falls out by injecting a `DryRunLedger`.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from firefly_bot.config import Settings
from firefly_bot.firefly.client import FireflyClient
from firefly_bot.firefly.ledger import DryRunLedger, Ledger
from firefly_bot.ingest.source import AttachmentSource, ImapAttachmentSource
from firefly_bot.matching.matcher import match_invoice
from firefly_bot.models import (
    Attachment,
    ExtractedInvoice,
    FireflyTransaction,
    MatchOutcome,
    MatchResult,
)
from firefly_bot.ocr.extract import RapidOcrTextRecogniser, TextRecogniser, extract_invoice
from firefly_bot.report.summary import ReportWriter, XlsxReportWriter

log = logging.getLogger("firefly_bot")


def run(
    settings: Settings,
    *,
    source: AttachmentSource | None = None,
    recogniser: TextRecogniser | None = None,
    ledger: Ledger | None = None,
    report_writer: ReportWriter | None = None,
    dry_run: bool = False,
) -> str:
    """Execute one full pass. Returns the path to the audit report.

    Any dependency left as None is constructed from `settings`. When `dry_run` is set and no
    ledger is injected, the real client is wrapped in a `DryRunLedger` so it reads live data
    but writes nothing.
    """
    source = source or ImapAttachmentSource(settings.imap)
    recogniser = recogniser or RapidOcrTextRecogniser(dpi=settings.ocr_dpi)
    report_writer = report_writer or XlsxReportWriter()

    owns_ledger = ledger is None
    if ledger is None:
        client = FireflyClient(settings.firefly)
        ledger = DryRunLedger(client) if dry_run else client

    try:
        attachments = _dedup(source.fetch())
        log.info("Fetched %d unique attachment(s)", len(attachments))
        if not attachments:
            return report_writer.write([], settings.report_dir)

        invoices = [extract_invoice(att, recogniser) for att in attachments]
        window = _window_for(invoices, settings.matching.date_window_days)
        transactions = ledger.list_transactions(start=window[0], end=window[1])
        log.info("Loaded %d candidate transaction(s)", len(transactions))

        results = [_process(inv, transactions, ledger, settings) for inv in invoices]
        path = report_writer.write(results, settings.report_dir)
        log.info("Wrote report: %s", path)
        return path
    finally:
        if owns_ledger:
            ledger.close()


def _process(
    invoice: ExtractedInvoice,
    transactions: list[FireflyTransaction],
    ledger: Ledger,
    settings: Settings,
) -> MatchResult:
    result = match_invoice(invoice, transactions, settings.matching)
    if result.transaction is None:
        return result
    try:
        ledger.attach_document(result.transaction, invoice.source)
        tags = [settings.matching.processed_tag]
        if result.outcome is MatchOutcome.ATTACHED_NEEDS_REVIEW:
            tags.append(settings.matching.needs_review_tag)
        ledger.add_tags(result.transaction, tags)
    except Exception as exc:  # noqa: BLE001 - report, never crash the whole run
        log.exception("Failed to act on %s", invoice.source.filename)
        return result.model_copy(
            update={"outcome": MatchOutcome.ERROR, "detail": f"{result.detail} | error: {exc}"}
        )
    return result


def _dedup(attachments: list[Attachment]) -> list[Attachment]:
    seen: set[str] = set()
    unique: list[Attachment] = []
    for att in attachments:
        if att.sha256 not in seen:
            seen.add(att.sha256)
            unique.append(att)
    return unique


def _window_for(invoices: list[ExtractedInvoice], pad_days: int) -> tuple[date, date]:
    dates = [inv.invoice_date for inv in invoices if inv.invoice_date is not None]
    received = [inv.source.received_at.date() for inv in invoices]
    anchors = dates + received
    lo = min(anchors)
    hi = max(anchors)
    return lo - timedelta(days=pad_days), hi + timedelta(days=pad_days)
