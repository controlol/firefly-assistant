"""Rasterise document bytes to images for OCR.

Kept separate from `extract.py` (and free of any PaddleOCR import) so the rasterisation can be
unit-tested on its own. Returns BGR uint8 ndarrays — the channel order PaddleOCR/OpenCV expect.
"""

from __future__ import annotations

import io

import numpy as np
from numpy.typing import NDArray

BGRImage = NDArray[np.uint8]


def _pil_to_bgr(img: object) -> BGRImage:
    """Convert a PIL image to a BGR uint8 ndarray. Single conversion path for all sources."""
    from PIL import Image

    assert isinstance(img, Image.Image)
    rgb = img.convert("RGB")
    arr = np.asarray(rgb, dtype=np.uint8)
    # RGB -> BGR; copy so downstream consumers get a contiguous, owned buffer.
    return arr[:, :, ::-1].copy()


def decode_image(data: bytes) -> BGRImage:
    """Decode PNG/JPEG bytes to a BGR ndarray."""
    from PIL import Image

    with Image.open(io.BytesIO(data)) as img:
        return _pil_to_bgr(img)


def pdf_to_images(data: bytes, dpi: int = 300) -> list[BGRImage]:
    """Rasterise every page of a PDF to a BGR ndarray at the given DPI."""
    import pypdfium2 as pdfium

    scale = dpi / 72.0
    pdf = pdfium.PdfDocument(data)
    try:
        images: list[BGRImage] = []
        for index in range(len(pdf)):
            page = pdf[index]
            bitmap = page.render(scale=scale)
            pil_image = bitmap.to_pil()
            images.append(_pil_to_bgr(pil_image))
            bitmap.close()
            page.close()
        return images
    finally:
        pdf.close()


def render(content_type: str, data: bytes, dpi: int = 300) -> list[BGRImage]:
    """Dispatch on content-type: rasterise PDFs, decode images."""
    if content_type == "application/pdf":
        return pdf_to_images(data, dpi=dpi)
    return [decode_image(data)]
