"""Read every page of every PDF once and keep the result on disk.

Each page's reading is saved as OUT/pages/<pdf key>/p0001.json the moment it is done. A job that runs out of time, or
is cancelled, therefore loses at most one page: run the same command again and it carries on where it stopped. The
workbook and CSVs are built from these files afterwards (`build`), which needs no GPU.
"""
from __future__ import annotations

import json
import os
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Protocol

from PIL import Image

from . import ocr as ocr_module
from .parse import is_figure, parse_reply
from .pdfpages import find_pdfs, page_count, pdf_key, render_pages
from .textcheck import MIN_TEXT, check_cells
from .verify import REREAD_THRESHOLD, verified_by, verify_page


class Reader(Protocol):
    name: str

    def read(self, image: Image.Image, page_text: str = ""): ...

    def free_memory(self) -> None: ...


def page_file(out_dir: Path, key: str, page: int) -> Path:
    return out_dir / "pages" / key / f"p{page:04d}.json"


def write_json(path: Path, data: dict) -> None:
    """Write to a temporary name and rename, so a job killed mid-write never leaves a half-written file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def select_shard(pdfs: list[Path], shard: tuple[int, int]) -> list[Path]:
    """Shard i of n takes every n-th PDF starting at i, so n GPU jobs split a folder without overlapping."""
    index, count = shard
    return pdfs[index::count]


def extract(
    pdf_dir: Path,
    out_dir: Path,
    reader: Reader,
    dpi: int = 200,
    max_pixels: int = 3_200_000,
    shard: tuple[int, int] = (0, 1),
    max_pages: int | None = None,
    retry_errors: bool = False,
    reread: bool = True,
    reread_threshold: float = REREAD_THRESHOLD,
    reread_max_pixels: int = 6_000_000,
    ocr: bool | Callable = True,
    log: Callable[[str], None] = print,
) -> dict:
    """Read the PDFs. ocr: True uses Tesseract when it is installed, False never, or pass an OCR function (for tests)."""
    ocr_function = ocr if callable(ocr) else (ocr_module.read_lines if ocr and ocr_module.tesseract_available() else None)
    pdfs = select_shard(find_pdfs(pdf_dir), shard)
    stats = {"pdfs": len(pdfs), "pages_read": 0, "pages_cached": 0, "tables": 0, "errors": 0}
    log(f"{len(pdfs)} PDF(s) in this job (shard {shard[0]} of {shard[1]}), model {reader.name}, "
        f"OCR {'on' if ocr_function else 'off'}, re-read of picture tables {'on' if reread else 'off'}")

    for i, pdf in enumerate(pdfs, 1):
        key = pdf_key(pdf, pdf_dir)
        rel = pdf.relative_to(pdf_dir).as_posix()
        # one record per PDF, so a PDF with no tables (or one that never finished) still shows up in the outputs
        document = out_dir / "pages" / key / "document.json"
        try:
            n_pages = page_count(pdf)
        except Exception as exc:  # encrypted, corrupt, or not really a PDF
            write_json(document, {"pdf": rel, "key": key, "n_pages": 0, "planned": 0, "error": f"cannot open: {exc}"})
            stats["errors"] += 1
            log(f"[{i}/{len(pdfs)}] {rel}: cannot open ({exc})")
            continue
        last = n_pages if max_pages is None else min(n_pages, max_pages)
        write_json(document, {"pdf": rel, "key": key, "n_pages": n_pages, "planned": last, "error": None})

        todo = [p for p in range(1, last + 1) if _needs_reading(page_file(out_dir, key, p), retry_errors)]
        stats["pages_cached"] += last - len(todo)
        log(f"[{i}/{len(pdfs)}] {rel}: {n_pages} page(s), {len(todo)} to read")

        for page, image, text in render_pages(pdf, todo, dpi, max_pixels):
            record = {"pdf": rel, "key": key, "page": page, "model": reader.name, "dpi": dpi,
                      "image_size": list(image.size), "text_layer": len(text.strip()) >= MIN_TEXT,
                      "error": None, "truncated": False, "tables": []}
            try:
                reply = reader.read(image, text)
                record.update(raw=reply.text, truncated=reply.truncated, seconds=round(reply.seconds, 1))
                tables = parse_reply(reply.text)
                # diagrams and trees the model still read as tables are left out, but named, so nothing vanishes
                record["skipped_figures"] = [t.label or t.caption[:80] for t in tables if is_figure(t)]
                record["tables"] = [asdict(t) for t in tables if not is_figure(t)]
                _verify(record, pdf, page, text, out_dir / "pages" / key, reader, ocr_function, reread, reread_threshold,
                        reread_max_pixels)
            except ValueError as exc:  # the reply was not usable JSON; the raw text is kept for a person to look at
                record["error"] = f"unreadable reply: {exc}"
            except Exception as exc:  # e.g. out of GPU memory on a huge page; record it and go on to the next page
                record["error"] = f"{type(exc).__name__}: {exc}"
                record["traceback"] = traceback.format_exc()
                reader.free_memory()

            if record["tables"]:
                # the page picture, so whoever checks a table can see what the model saw
                png = page_file(out_dir, key, page).with_suffix(".png")
                png.parent.mkdir(parents=True, exist_ok=True)
                image.save(png)
            write_json(page_file(out_dir, key, page), record)

            stats["pages_read"] += 1
            stats["tables"] += len(record["tables"])
            stats["errors"] += record["error"] is not None
            note = record["error"] or f"{len(record['tables'])} table(s)" + (" (CUT OFF)" if record["truncated"] else "")
            rereads = sum(1 for t in record["tables"] if t.get("reread"))
            if rereads:
                note += f", {rereads} re-read from the picture"
            log(f"    p{page}: {note}, {record.get('seconds', 0)}s")

    return stats


def _verify(record, pdf, page, text, out_dir_page, reader, ocr_function, reread, threshold, max_pixels) -> None:
    try:
        verify_page(record, pdf=pdf, page=page, text=text, out_dir_page=out_dir_page, reader=reader,
                    ocr_function=ocr_function, reread=reread, threshold=threshold, reread_max_pixels=max_pixels)
    except Exception as exc:  # verification is an extra: a failure here must never lose the page's tables
        record.setdefault("verify_notes", []).append(f"verification failed: {type(exc).__name__}: {exc}")
        record["verify_traceback"] = traceback.format_exc()
        for table in record["tables"]:
            check_cells(table, text)
            table["verified_by"] = verified_by(table)
    if not record.get("verify_notes"):
        record.pop("verify_notes", None)


def _needs_reading(path: Path, retry_errors: bool) -> bool:
    if not path.exists():
        return True
    if not retry_errors:
        return False
    data = json.loads(path.read_text(encoding="utf-8"))
    return bool(data.get("error") or data.get("truncated"))


def load_records(out_dir: Path) -> tuple[list[dict], list[dict]]:
    """All saved page readings, and one record per PDF the run has started on, in file order."""
    pages, documents = [], []
    for path in sorted((out_dir / "pages").glob("*/*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        (documents if path.name == "document.json" else pages).append(data)
    pages.sort(key=lambda r: (r["pdf"], r["page"]))
    documents.sort(key=lambda d: d["pdf"])
    return pages, documents
