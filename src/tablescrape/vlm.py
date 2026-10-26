"""The vision-language model that reads a page image. Runs locally through Hugging Face transformers; no API key."""
from __future__ import annotations

import time
from dataclasses import dataclass

from PIL import Image

DEFAULT_MODEL = "Qwen/Qwen3-VL-8B-Instruct"

PROMPT = """Find every table on this page image and copy it exactly.

A table is data laid out in rows and columns. That includes tables pasted as pictures, rotated tables, and the continuation of a table from the previous page. Figures, charts, plots, diagrams, phylogenetic trees, flowcharts, equations and ordinary paragraphs are not tables, even when their text lines up in a grid; anything captioned "Fig." or "Figure" is a figure.

Reply with JSON only, no other text, in exactly this form:
{"tables": [{"label": "Table 2", "caption": "...", "columns": ["...", "..."], "rows": [["...", "..."], ["...", "..."]], "footnotes": "...", "continued": false}]}

Rules:
- Copy every cell character for character as printed: keep digits, decimal places, signs, ±, units, Greek letters, superscript markers and abbreviations. Never round, convert, translate, correct or invent a value.
- Every cell is a JSON string. An empty cell is "".
- "columns" is the header. If the header has several levels, join the levels of each column from top to bottom with " | ". If no header row is printed on this page (for example a table continued from the previous page), "columns" is [] - never invent a header and never use the first column's values as headers.
- Each printed row is one row in "rows", read left to right. Row names (the first column) stay in the first cell of their row.
- Every row has exactly as many cells as the table has columns. A cell that spans several columns goes in its first column and the others are "". A value that spans several rows is repeated in each of those rows.
- A row that is a heading inside the table (for example a group name across the full width) is its own row, with the heading in the first cell.
- "label" is the table's printed number, such as "Table 3" or "Table S1"; "" if none is printed. "caption" is the title printed with the table; "" if none. "footnotes" is the notes printed directly under the table; "" if none.
- "continued" is true when the table continues from a previous page.
- If the page has no tables, reply {"tables": []}."""

TEXT_HINT = """

Below is the text stored inside the PDF for this page. Where a table's text appears in it, its characters are exact, while the image can be blurry, so copy characters (letters, digits, sequences, symbols) from this text. Take the table's layout (which cell is in which row and column) from the image, because the text below is often in the wrong order and may run cells together. The text may also hold parts of the page that are not tables, and lacks tables that are pasted pictures.

<pdf_text>
{text}
</pdf_text>"""

# Enough for a dense page; more only costs time, since a page's text rarely exceeds this.
MAX_HINT_CHARS = 12000


def build_prompt(page_text: str) -> str:
    """The prompt, with the page's text layer appended when it has one worth reading."""
    text = page_text.strip()
    if len(text) < 20:  # a picture-only page, or a page number and nothing else
        return PROMPT
    return PROMPT + TEXT_HINT.format(text=text[:MAX_HINT_CHARS])


@dataclass
class Reply:
    text: str
    truncated: bool  # the reply hit max_new_tokens, so the end of a long table may be missing
    seconds: float


class QwenVLReader:
    """Qwen-VL family (Qwen3-VL, Qwen2.5-VL) through transformers' generic image-text-to-text classes."""

    def __init__(self, model_id: str = DEFAULT_MODEL, max_new_tokens: int = 8192):
        import torch
        from transformers import AutoModelForImageTextToText, AutoProcessor

        self.name = model_id
        self.max_new_tokens = max_new_tokens
        self._torch = torch
        self.processor = AutoProcessor.from_pretrained(model_id)
        self.model = AutoModelForImageTextToText.from_pretrained(model_id, dtype=torch.bfloat16, device_map="auto")
        self.model.eval()

    def read(self, image: Image.Image, page_text: str = "") -> Reply:
        torch = self._torch
        prompt = build_prompt(page_text)
        messages = [{"role": "user", "content": [{"type": "image", "image": image}, {"type": "text", "text": prompt}]}]
        inputs = self.processor.apply_chat_template(
            messages, tokenize=True, add_generation_prompt=True, return_dict=True, return_tensors="pt"
        ).to(self.model.device)
        start = time.time()
        with torch.inference_mode():
            # greedy decoding: the same page gives the same reading every run, and nothing is "creatively" sampled
            out = self.model.generate(**inputs, max_new_tokens=self.max_new_tokens, do_sample=False)
        new_tokens = out[0, inputs["input_ids"].shape[1]:]
        text = self.processor.decode(new_tokens, skip_special_tokens=True)
        return Reply(text=text, truncated=len(new_tokens) >= self.max_new_tokens, seconds=time.time() - start)

    def free_memory(self) -> None:
        """Called after an error such as running out of GPU memory, so the next page starts clean."""
        if self._torch.cuda.is_available():
            self._torch.cuda.empty_cache()


def cuda_available() -> bool:
    try:
        import torch
    except ImportError:
        return False
    return torch.cuda.is_available()
