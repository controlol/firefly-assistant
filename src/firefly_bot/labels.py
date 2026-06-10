"""Append-only capture of every decision the pipeline makes — the Phase 1 labelling surface.

`LabelStore` is an injected Protocol, mirroring `Ledger` / `ReportWriter`: the real
`JsonlLabelStore` appends one compact JSON line per `LabelRecord`, and `NullLabelStore` is the
write-suppressing no-op used for `--dry-run` and tests (the same shape as `DryRunLedger`).

No model consumes these records yet; Phase 1 only accumulates them at `./data/labels.jsonl`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Protocol

from firefly_bot.models import LabelRecord

log = logging.getLogger("firefly_bot.labels")


def read_labels(path: str | Path) -> Iterator[LabelRecord]:
    """Yield every `LabelRecord` already persisted at ``path`` (empty if the file is absent).

    Used by the bootstrap importer to load prior seeds for idempotency. Blank lines are ignored.
    """
    file = Path(path)
    if not file.exists():
        return
    with file.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield LabelRecord.model_validate_json(line)


class LabelStore(Protocol):
    """Sink for training examples. Everything the pipeline/importer needs to record a decision."""

    def record(self, record: LabelRecord) -> None: ...

    def close(self) -> None: ...


class JsonlLabelStore:
    """Append-only JSONL store: one compact `LabelRecord` JSON object per line.

    The parent directory is created on first write. The file handle is opened lazily so that
    constructing the store (e.g. from settings) never touches the filesystem until a record is
    actually written.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._handle: IO[str] | None = None

    def _ensure_open(self) -> IO[str]:
        if self._handle is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self._path.open("a", encoding="utf-8")
        return self._handle

    def record(self, record: LabelRecord) -> None:
        handle = self._ensure_open()
        handle.write(record.model_dump_json() + "\n")

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


class NullLabelStore:
    """No-op store: records nothing. Used for `--dry-run` and tests that should not write."""

    def record(self, record: LabelRecord) -> None:
        log.debug("[null-store] would record %s label", record.kind)

    def close(self) -> None:
        return None
