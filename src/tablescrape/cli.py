"""tablescrape: copy every table in a folder of PDFs into a labeled workbook and CSVs.

    tablescrape list  PDF_DIR                 how many PDFs and pages (cheap; fine on a login node)
    tablescrape run   PDF_DIR -o OUT          read every page with the vision model (GPU node), then build
    tablescrape build OUT                     rebuild tables.xlsx / CSVs from what has been read (no GPU)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return args.func(args)


def cmd_list(args) -> int:
    from .pdfpages import find_pdfs, page_count

    root = Path(args.pdf_dir)
    total = 0
    for pdf in find_pdfs(root):
        try:
            n = page_count(pdf)
        except Exception as exc:
            print(f"   ?  {pdf.relative_to(root)}  (cannot open: {exc})")
            continue
        total += n
        print(f"{n:5d}  {pdf.relative_to(root)}")
    print(f"{total:5d}  pages in total")
    return 0


def cmd_run(args) -> int:
    from .pipeline import extract
    from .vlm import QwenVLReader, cuda_available

    pdf_dir, out_dir = Path(args.pdf_dir), Path(args.output)
    if not pdf_dir.is_dir():
        print(f"error: {pdf_dir} is not a directory", file=sys.stderr)
        return 2
    if not cuda_available() and not args.allow_cpu:
        # This is what keeps the model off a login node: it only loads where Slurm has handed us a GPU.
        print("error: no GPU visible. Run this inside a Slurm job (sbatch hpc/itiger/scrape.sbatch ... or "
              "srun --gres=gpu:1 ...). --allow-cpu overrides this, but an 8B model on CPU takes minutes per page.",
              file=sys.stderr)
        return 3

    reader = QwenVLReader(args.model, max_new_tokens=args.max_new_tokens)
    stats = extract(pdf_dir, out_dir, reader, dpi=args.dpi, max_pixels=args.max_pixels, shard=args.shard,
                    max_pages=args.max_pages, retry_errors=args.retry_errors, reread=not args.no_reread,
                    reread_threshold=args.reread_threshold, reread_max_pixels=args.reread_max_pixels,
                    ocr=not args.no_ocr, log=lambda m: print(m, flush=True))
    print(f"read {stats['pages_read']} page(s) ({stats['pages_cached']} already done), "
          f"{stats['tables']} table(s), {stats['errors']} error(s)")
    if args.shard[1] > 1:
        print("sharded run: build the workbook once every shard has finished: tablescrape build " + str(out_dir))
    elif not args.no_build:
        cmd_build(argparse.Namespace(output=str(out_dir), no_numbers=args.no_numbers))
    return 0


def cmd_build(args) -> int:
    from .export import build

    out_dir = Path(args.output)
    stats = build(out_dir, numbers=not args.no_numbers)
    print(f"{stats['tables']} table(s) from {stats['pages']} page(s) of {stats['pdfs']} PDF(s) -> "
          f"{out_dir / 'tables.xlsx'}, {out_dir / 'csv'}/, index.csv, documents.csv; "
          f"{stats['problems']} problem(s) listed in the Problems sheet")
    return 0


def _shard(text: str) -> tuple[int, int]:
    try:
        i, n = (int(x) for x in text.split("/"))
    except ValueError:
        raise argparse.ArgumentTypeError("use I/N, e.g. 0/4") from None
    if not (n >= 1 and 0 <= i < n):
        raise argparse.ArgumentTypeError("need 0 <= I < N")
    return i, n


def _parser() -> argparse.ArgumentParser:
    from .vlm import DEFAULT_MODEL

    p = argparse.ArgumentParser(prog="tablescrape", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(required=True)

    ls = sub.add_parser("list", help="count the PDFs and pages in a folder")
    ls.add_argument("pdf_dir")
    ls.set_defaults(func=cmd_list)

    run = sub.add_parser("run", help="read every page with the vision model, then build the outputs")
    run.add_argument("pdf_dir", help="folder of PDFs (searched recursively)")
    run.add_argument("-o", "--output", required=True, help="output folder (created; rerunning resumes)")
    run.add_argument("--model", default=DEFAULT_MODEL, help=f"Hugging Face id or local path (default {DEFAULT_MODEL})")
    run.add_argument("--dpi", type=int, default=200, help="page render resolution (default 200)")
    run.add_argument("--max-pixels", type=int, default=3_200_000,
                     help="shrink page images to at most this many pixels (default 3200000)")
    run.add_argument("--max-new-tokens", type=int, default=8192, help="longest reply per page (default 8192)")
    run.add_argument("--shard", type=_shard, default=(0, 1), metavar="I/N",
                     help="only PDFs I, I+N, I+2N, ... (split a folder across N GPU jobs)")
    run.add_argument("--max-pages", type=int, help="only the first N pages of each PDF (for a quick test)")
    run.add_argument("--retry-errors", action="store_true", help="re-read pages that failed or were cut off")
    run.add_argument("--no-reread", action="store_true",
                     help="do not re-read tables that look like pictures from the original picture in the PDF")
    run.add_argument("--reread-threshold", type=float, default=0.25, metavar="SHARE",
                     help="a table counts as a picture table when at least this share of its cells is not in the "
                          "PDF's text (default 0.25)")
    run.add_argument("--reread-max-pixels", type=int, default=6_000_000,
                     help="shrink a picture to at most this many pixels before the model reads it (default 6000000)")
    run.add_argument("--no-ocr", action="store_true", help="do not use Tesseract OCR as a second reader")
    run.add_argument("--no-build", action="store_true", help="do not build tables.xlsx at the end")
    run.add_argument("--no-numbers", action="store_true", help="keep every workbook cell as text")
    run.add_argument("--allow-cpu", action="store_true", help="run without a GPU (very slow)")
    run.set_defaults(func=cmd_run)

    b = sub.add_parser("build", help="build tables.xlsx, CSVs and index.csv from the pages read so far")
    b.add_argument("output", help="the output folder given to run")
    b.add_argument("--no-numbers", action="store_true", help="keep every workbook cell as text")
    b.set_defaults(func=cmd_build)
    return p


if __name__ == "__main__":
    sys.exit(main())
