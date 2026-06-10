"""OCR extraction: raw bytes -> ExtractedInvoice.

The default engine is RapidOCR (PP-OCR models on ONNXRuntime): no paddlepaddle dependency,
models ship in the wheel, and it runs reliably on Windows/CPU. `PaddleTextRecogniser` is kept
as an optional alternative (install the `paddle` extra), but paddle 3.x currently fails to run
the PP-OCRv3 graphs on CPU, so RapidOCR is preferred.

For total + IBAN, flat text is sufficient; the PP-Structure "two-pass" (layout block detection
then per-block extraction) is the planned iteration-2 upgrade. Engines are imported lazily so
the rest of the package (and the pure-logic tests) need neither dependency installed.
"""

from __future__ import annotations

from typing import Protocol

from firefly_bot.models import Attachment, ExtractedInvoice
from firefly_bot.ocr.heuristics import extract_iban, extract_total
from firefly_bot.ocr.render import render


class TextRecogniser(Protocol):
    """Anything that turns document bytes into plain text. Lets us swap/fake the OCR engine."""

    def to_text(self, attachment: Attachment) -> str: ...


class RapidOcrTextRecogniser:
    """Default recogniser: RapidOCR (PP-OCR on ONNXRuntime). CPU-friendly, no paddlepaddle."""

    def __init__(self, *, dpi: int = 200) -> None:
        from rapidocr_onnxruntime import RapidOCR  # lazy import

        self._engine = RapidOCR()
        self._dpi = dpi

    def to_text(self, attachment: Attachment) -> str:
        lines: list[str] = []
        for image in render(attachment.content_type, attachment.data, dpi=self._dpi):
            result, _elapse = self._engine(image)
            if result:
                # RapidOCR rows are [box, text, score].
                lines.extend(str(row[1]) for row in result)
        return "\n".join(lines)


class PaddleTextRecogniser:
    """PaddleOCR-backed recogniser. Handles both images and (rendered) PDF pages."""

    def __init__(self, *, use_gpu: bool = False, lang: str = "nl", dpi: int = 300) -> None:
        from paddleocr import PaddleOCR  # lazy import

        self._ocr = PaddleOCR(use_angle_cls=True, lang=lang, use_gpu=use_gpu, show_log=False)
        self._dpi = dpi

    def to_text(self, attachment: Attachment) -> str:
        images = render(attachment.content_type, attachment.data, dpi=self._dpi)
        lines: list[str] = []
        for image in images:
            result = self._ocr.ocr(image, cls=True)
            lines.extend(self._flatten(result))
        return "\n".join(lines)

    @staticmethod
    def _flatten(result: object) -> list[str]:
        """Pull the recognised strings out of PaddleOCR's nested result structure."""
        texts: list[str] = []
        if not isinstance(result, list):
            return texts
        for page in result:
            if not isinstance(page, list):
                continue
            for line in page:
                # line == [box, (text, confidence)]
                if isinstance(line, list) and len(line) == 2 and isinstance(line[1], tuple):
                    texts.append(str(line[1][0]))
        return texts


def extract_invoice(attachment: Attachment, recogniser: TextRecogniser) -> ExtractedInvoice:
    """Run OCR + heuristics to produce a typed ExtractedInvoice."""
    text = recogniser.to_text(attachment)
    total, total_conf = extract_total(text)
    iban, iban_conf = extract_iban(text)
    return ExtractedInvoice(
        source=attachment,
        total_amount=total,
        counterparty_iban=iban,
        raw_text=text,
        total_confidence=total_conf,
        iban_confidence=iban_conf,
    )
