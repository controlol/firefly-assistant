"""Orchestrates a single run: ingest -> extract -> match -> act -> report.

Every I/O boundary (attachment source, OCR recogniser, Firefly ledger, report writer) is an
injected Protocol with a real default, so `run` is fully testable with fakes and `--dry-run`
falls out by injecting a `DryRunLedger`.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

from firefly_bot.config import Settings
from firefly_bot.firefly.client import FireflyClient
from firefly_bot.firefly.ledger import DryRunLedger, Ledger
from firefly_bot.ingest.source import AttachmentSource, ImapAttachmentSource
from firefly_bot.labels import JsonlLabelStore, LabelStore, NullLabelStore
from firefly_bot.matching.matcher import match_invoice, score_candidates
from firefly_bot.models import (
    Attachment,
    ExtractedInvoice,
    FireflyTransaction,
    LabelRecord,
    MatchOutcome,
    MatchResult,
)
from firefly_bot.ocr.extract import RapidOcrTextRecogniser, TextRecogniser, extract_invoice
from firefly_bot.report.summary import ReportWriter, XlsxReportWriter
from firefly_bot.ubl import embedded_pdf, is_ubl_document

log = logging.getLogger("firefly_bot")

# Human-readable documents we attach to Firefly (the raw UBL XML is data, not attached as-is).
_ATTACHABLE_TYPES = frozenset({"application/pdf", "image/png", "image/jpeg"})

# Outcomes where the document was attached to a transaction (so the source can be marked done).
_ATTACHED_OUTCOMES = frozenset({MatchOutcome.AUTO_ATTACHED, MatchOutcome.ATTACHED_NEEDS_REVIEW})


def run(
    settings: Settings,
    *,
    source: AttachmentSource | None = None,
    recogniser: TextRecogniser | None = None,
    ledger: Ledger | None = None,
    report_writer: ReportWriter | None = None,
    label_store: LabelStore | None = None,
    dry_run: bool = False,
) -> str:
    """Execute one full pass. Returns the path to the audit report.

    Any dependency left as None is constructed from `settings`. When `dry_run` is set and no
    ledger is injected, the real client is wrapped in a `DryRunLedger` so it reads live data
    but writes nothing — and the `label_store` defaults to a `NullLabelStore` for the same reason.
    """
    source = source or ImapAttachmentSource(settings.imap)
    recogniser = recogniser or RapidOcrTextRecogniser(dpi=settings.ocr_dpi)
    report_writer = report_writer or XlsxReportWriter()

    owns_store = label_store is None
    if label_store is None:
        label_store = NullLabelStore() if dry_run else JsonlLabelStore(settings.labels_path)

    owns_ledger = ledger is None
    if ledger is None:
        client = FireflyClient(settings.firefly)
        ledger = DryRunLedger(client) if dry_run else client

    try:
        attachments = _dedup(source.fetch())
        log.info("Fetched %d unique attachment(s)", len(attachments))
        if not attachments:
            return report_writer.write([], settings.report_dir)

        invoices = _group_invoices(attachments, recogniser)
        window = _window_for(invoices, settings.matching.date_window_days)
        transactions = ledger.list_transactions(start=window[0], end=window[1])
        log.info("Loaded %d candidate transaction(s)", len(transactions))

        results: list[MatchResult] = []
        for inv in invoices:
            result = _process(inv, transactions, ledger, label_store, settings)
            if result.outcome in _ATTACHED_OUTCOMES:
                source.mark_processed(inv.source)
            else:
                # Couldn't attach — flag for manual investigation, leave for retry.
                source.flag_unprocessed(inv.source)
            results.append(result)

        unresolved = sum(1 for r in results if r.outcome not in _ATTACHED_OUTCOMES)
        if unresolved:
            log.warning("%d document(s) not attached — left for retry/review", unresolved)
        path = report_writer.write(results, settings.report_dir)
        log.info("Wrote report: %s", path)
        return path
    finally:
        source.close()
        if owns_ledger:
            ledger.close()
        if owns_store:
            label_store.close()


def _process(
    invoice: ExtractedInvoice,
    transactions: list[FireflyTransaction],
    ledger: Ledger,
    label_store: LabelStore,
    settings: Settings,
) -> MatchResult:
    result = match_invoice(invoice, transactions, settings.matching)
    _capture_match_labels(invoice, transactions, result, label_store, settings)
    if result.transaction is None:
        return result
    try:
        for document in invoice.attachables:
            ledger.attach_document(result.transaction, document)
        # Preserve the transaction's existing tags — append ours, don't replace.
        tags = [*result.transaction.tags, settings.matching.processed_tag]
        if result.outcome is MatchOutcome.ATTACHED_NEEDS_REVIEW:
            tags.append(settings.matching.needs_review_tag)
        ledger.add_tags(result.transaction, list(dict.fromkeys(tags)))
    except Exception as exc:  # noqa: BLE001 - report, never crash the whole run
        log.exception("Failed to act on %s", invoice.source.filename)
        return result.model_copy(
            update={"outcome": MatchOutcome.ERROR, "detail": f"{result.detail} | error: {exc}"}
        )
    return result


def _capture_match_labels(
    invoice: ExtractedInvoice,
    transactions: list[FireflyTransaction],
    result: MatchResult,
    label_store: LabelStore,
    settings: Settings,
) -> None:
    """Emit one `match` LabelRecord per positively-scored candidate (winner + losers).

    `predicted` is the chosen transaction id (same for every record of this decision); the
    per-candidate `score` and the `chosen` flag let a future trainer form positive/negative pairs.
    """
    chosen_id = result.transaction.id if result.transaction else None
    ts = datetime.now(tz=UTC)
    for txn, score, features in score_candidates(invoice, transactions, settings.matching):
        label_store.record(
            LabelRecord(
                ts=ts,
                kind="match",
                features={**features, "candidate_id": txn.id, "chosen": txn.id == chosen_id},
                predicted=chosen_id,
                score=score,
                source="auto",
            )
        )


def _dedup(attachments: list[Attachment]) -> list[Attachment]:
    seen: set[str] = set()
    unique: list[Attachment] = []
    for att in attachments:
        if att.sha256 not in seen:
            seen.add(att.sha256)
            unique.append(att)
    return unique


def _group_invoices(
    attachments: list[Attachment], recogniser: TextRecogniser
) -> list[ExtractedInvoice]:
    """One invoice per email: extract from the UBL when present, but always attach the PDF."""
    groups: dict[str, list[Attachment]] = {}
    for att in attachments:
        groups.setdefault(att.source_message_id, []).append(att)
    return [_invoice_for_group(group, recogniser) for group in groups.values()]


def _invoice_for_group(
    group: list[Attachment], recogniser: TextRecogniser
) -> ExtractedInvoice:
    ubl = next((att for att in group if is_ubl_document(att)), None)
    readable = [att for att in group if att.content_type in _ATTACHABLE_TYPES]
    extraction_source = ubl or (readable[0] if readable else group[0])
    invoice = extract_invoice(extraction_source, recogniser)

    documents = list(readable)
    if ubl is not None and not any(d.content_type == "application/pdf" for d in documents):
        # Read data from the UBL but always attach a PDF — the one embedded in the XML.
        pdf = embedded_pdf(ubl)
        if pdf is not None:
            documents.insert(0, pdf)
    if not documents:
        documents = [extraction_source]  # last resort: e.g. a UBL with no embedded PDF
    return invoice.model_copy(update={"documents": tuple(documents)})


def _window_for(invoices: list[ExtractedInvoice], pad_days: int) -> tuple[date, date]:
    dates = [inv.invoice_date for inv in invoices if inv.invoice_date is not None]
    received = [inv.source.received_at.date() for inv in invoices]
    anchors = dates + received
    lo = min(anchors)
    hi = max(anchors)
    return lo - timedelta(days=pad_days), hi + timedelta(days=pad_days)
