"""Tests for the rasterisation module (no PaddleOCR needed).

A sample PDF and PNG are generated in-memory so the test is self-contained.
"""

from __future__ import annotations

import io

import numpy as np
import pypdfium2 as pdfium
from PIL import Image

from firefly_bot.ocr.render import decode_image, pdf_to_images, render


def _sample_pdf(pages: int = 2, size: int = 200) -> bytes:
    pdf = pdfium.PdfDocument.new()
    try:
        for _ in range(pages):
            pdf.new_page(float(size), float(size))
        buf = io.BytesIO()
        pdf.save(buf)
        return buf.getvalue()
    finally:
        pdf.close()


def _sample_png(width: int = 64, height: int = 48) -> bytes:
    img = Image.new("RGB", (width, height), color=(10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_pdf_renders_one_bgr_image_per_page() -> None:
    images = pdf_to_images(_sample_pdf(pages=2), dpi=72)
    assert len(images) == 2
    for arr in images:
        assert arr.dtype == np.uint8
        assert arr.ndim == 3 and arr.shape[2] == 3  # H, W, BGR


def test_decode_image_preserves_dimensions_and_bgr_order() -> None:
    arr = decode_image(_sample_png(width=64, height=48))
    assert arr.shape == (48, 64, 3)
    # RGB(10,20,30) -> BGR(30,20,10)
    assert tuple(int(c) for c in arr[0, 0]) == (30, 20, 10)


def test_render_dispatches_on_content_type() -> None:
    assert len(render("application/pdf", _sample_pdf(pages=1), dpi=72)) == 1
    assert len(render("image/png", _sample_png())) == 1
