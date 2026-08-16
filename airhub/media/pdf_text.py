"""PDF text extraction helpers."""

from __future__ import annotations

import re
from pathlib import Path


def extract_pdf_first_page_text(pdf_path: Path, max_chars: int = 20000) -> str:
    import fitz

    with fitz.open(pdf_path) as document:
        if len(document) == 0:
            return ""
        text = document[0].get_text("text")
    return re.sub(r"\s+", " ", text).strip()[:max_chars]
