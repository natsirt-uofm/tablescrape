"""Find the PDFs in a folder and render their pages to images."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
from PIL import Image


def find_pdfs(root: Path) -> list[Path]:
    """Every .pdf under root, at any depth and with any case of extension, in a stable order."""
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf")


def pdf_key(pdf: Path, root: Path) -> str:
    """A name for the PDF that is unique within the folder and safe in file names: papers/a b.pdf -> papers__a_b."""
    parts = pdf.relative_to(root).with_suffix("").parts
    return re.sub(r"[^A-Za-z0-9._-]+", "_", "__".join(parts))


def page_count(pdf: Path) -> int:
    doc = pdfium.PdfDocument(str(pdf))
    try:
        return len(doc)
    finally:
        doc.close()


def render_pages(pdf: Path, pages: Iterable[int], dpi: int,
                 max_pixels: int) -> Iterator[tuple[int, Image.Image, str]]:
    """Yield (page number counted from 1, RGB image, the page's text layer) for the requested pages.

    The text layer is the characters stored in the PDF itself: exact where it exists (born-digital PDFs and most
    publisher scans), empty for a page that is only a picture.
    """
    doc = pdfium.PdfDocument(str(pdf))
    try:
        for number in pages:
            page = doc[number - 1]
            image = page.render(scale=dpi / 72).to_pil().convert("RGB")
            textpage = page.get_textpage()
            text = textpage.get_text_range()
            textpage.close()
            page.close()
            yield number, fit_pixels(image, max_pixels), text
    finally:
        doc.close()


def fit_pixels(image: Image.Image, max_pixels: int) -> Image.Image:
    """Shrink the image (keeping its shape) so it has at most max_pixels; the model's cost grows with pixel count."""
    w, h = image.size
    if w * h <= max_pixels:
        return image
    s = math.sqrt(max_pixels / (w * h))
    return image.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)


@dataclass
class PageImage:
    """A picture stored inside a PDF page (a table pasted as an image is one of these)."""

    image: Image.Image  # RGB, at the picture's own resolution, capped to max_pixels
    bbox: tuple[float, float, float, float]  # left, bottom, right, top, in PDF points
    native_size: tuple[int, int]  # pixels, as stored in the PDF


def page_images(pdf: Path, number: int, max_pixels: int, min_width: float = 0.15, min_height: float = 0.06,
                limit: int = 3) -> list[PageImage]:
    """The larger pictures on a page, largest first.

    A table pasted into a paper as a picture is stored as a raster image, often at several times the resolution the
    page render gives the model. Reading that picture on its own, enlarged, gives the model far more detail than a
    shrunken full page does. Pictures smaller than min_width x min_height of the page (logos, icons) are ignored.
    """
    doc = pdfium.PdfDocument(str(pdf))
    found: list[PageImage] = []
    try:
        page = doc[number - 1]
        width, height = page.get_size()
        for obj in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE]):
            left, bottom, right, top = obj.get_bounds()
            if (right - left) < min_width * width or (top - bottom) < min_height * height:
                continue
            try:
                picture = obj.get_bitmap(render=False).to_pil().convert("RGB")
            except Exception:  # an image format pdfium will not hand over as a bitmap: render that part of the page
                scale = 300 / 72
                full = page.render(scale=scale).to_pil().convert("RGB")
                picture = full.crop((round(left * scale), round((height - top) * scale),
                                     round(right * scale), round((height - bottom) * scale)))
            native = picture.size
            found.append(PageImage(fit_pixels(picture, max_pixels), (left, bottom, right, top), native))
        page.close()
    finally:
        doc.close()
    found.sort(key=lambda p: -(p.bbox[2] - p.bbox[0]) * (p.bbox[3] - p.bbox[1]))
    return found[:limit]


def page_size(pdf: Path, number: int) -> tuple[float, float]:
    """The page's width and height in PDF points (1/72 inch)."""
    doc = pdfium.PdfDocument(str(pdf))
    try:
        return doc[number - 1].get_size()
    finally:
        doc.close()


def find_text_boxes(pdf: Path, number: int, queries: list[str]) -> dict[str, tuple[float, float, float, float]]:
    """Where each query string first appears in the page's stored text: left, bottom, right, top in PDF points."""
    doc = pdfium.PdfDocument(str(pdf))
    boxes: dict[str, tuple[float, float, float, float]] = {}
    try:
        page = doc[number - 1]
        textpage = page.get_textpage()
        for query in queries:
            if not query:
                continue
            hit = textpage.search(query).get_next()
            if hit is None:
                continue
            index, count = hit
            rects = [textpage.get_charbox(i) for i in range(index, index + count)]
            boxes[query] = (min(r[0] for r in rects), min(r[1] for r in rects),
                            max(r[2] for r in rects), max(r[3] for r in rects))
        textpage.close()
        page.close()
    finally:
        doc.close()
    return boxes
