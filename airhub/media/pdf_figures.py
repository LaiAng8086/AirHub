"""Server-side PDF figure extraction.

This is the Producer-owned version of the former paper-digest helper: it prepares
local figure assets and a manifest before any skill is invoked.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path
from typing import Any


FIGURE_CAPTION_RE = re.compile(
    r"\s*Fig(?:ure)?\.?\s+(\d+)(?:(?:\s*[:.]\s*)|\s+)(?=\S)",
    re.IGNORECASE,
)


def extract_pdf_figures(
    pdf_path: Path,
    out_dir: Path,
    dpi_scale: float = 2.2,
    max_width: int = 980,
    pad: float = 4.0,
) -> list[dict[str, Any]]:
    # Pillow's bundled libLerc requires a newer libstdc++ than the system copy.
    # Import it before PyMuPDF, which may otherwise preload the older library.
    from PIL import Image
    import fitz

    doc = fitz.open(pdf_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    cap_re = FIGURE_CAPTION_RE

    def text_blocks(page: Any) -> list[dict[str, Any]]:
        return [block for block in page.get_text("dict")["blocks"] if "lines" in block]

    def block_text(block: dict[str, Any]) -> str:
        # Preserve a separator between PDF text lines.  Without it, a caption
        # such as ``Figure 2`` followed by ``3D Scene...`` becomes
        # ``Figure 23D Scene...`` and the figure number is parsed as 23.
        return " ".join(
            "".join(span["text"] for span in line["spans"]).strip()
            for line in block["lines"]
        ).strip()

    def caption_blocks(page: Any) -> dict[int, Any]:
        caps: dict[int, Any] = {}
        for block in text_blocks(page):
            match = cap_re.match(block_text(block))
            if match:
                caps.setdefault(int(match.group(1)), fitz.Rect(block["bbox"]))
        return caps

    def full_caption(page: Any, fig_num: int) -> tuple[Any | None, str]:
        blocks = sorted(text_blocks(page), key=lambda block: block["bbox"][1])
        idx = next(
            (
                i
                for i, block in enumerate(blocks)
                if cap_re.match(block_text(block))
                and int(cap_re.match(block_text(block)).group(1)) == fig_num
            ),
            None,
        )
        if idx is None:
            return None, ""
        rect = fitz.Rect(blocks[idx]["bbox"])
        parts = [block_text(blocks[idx]).strip()]
        for next_block in blocks[idx + 1 :]:
            next_rect = fitz.Rect(next_block["bbox"])
            if cap_re.match(block_text(next_block)) or next_rect.y0 - rect.y1 > 7:
                break
            rect |= next_rect
            parts.append(block_text(next_block).strip())
        return rect, " ".join(parts).strip()

    def raster_rects(page: Any) -> list[Any]:
        return [fitz.Rect(item["bbox"]) for item in page.get_image_info()]

    def visuals(page: Any) -> list[Any]:
        rects = raster_rects(page)
        for drawing in page.get_drawings():
            rect = fitz.Rect(drawing["rect"])
            if rect.width > 5 and rect.height > 5:
                rects.append(rect)
        return rects

    def page_layout(caps: dict[int, Any], visual_rects: list[Any]) -> str:
        if not caps or not visual_rects:
            return "below"
        return "above" if min(c.y0 for c in caps.values()) < min(r.y0 for r in visual_rects) else "below"

    fig_page: dict[int, int] = {}
    for page_number in range(len(doc)):
        for fig_num in caption_blocks(doc[page_number]):
            fig_page.setdefault(fig_num, page_number)

    manifest: list[dict[str, Any]] = []
    for fig_num in sorted(fig_page):
        page_number = fig_page[fig_num]
        page = doc[page_number]
        caps = caption_blocks(page)
        visual_rects = visuals(page)
        layout = page_layout(caps, visual_rects)
        cap_rect, cap_text = full_caption(page, fig_num)
        current_cap = caps[fig_num]

        if layout == "above":
            lowers = [caps[num].y0 for num in caps if caps[num].y0 > current_cap.y1]
            bound = min(lowers) if lowers else page.rect.y1 - 36
            selected = [rect for rect in visual_rects if rect.y0 >= current_cap.y1 - 2 and rect.y1 <= bound + 2]
        else:
            uppers = [caps[num].y1 for num in caps if caps[num].y1 < current_cap.y0]
            bound = max(uppers) if uppers else page.rect.y0 + 28
            selected = [rect for rect in visual_rects if rect.y1 <= current_cap.y0 + 2 and rect.y0 >= bound - 2]

        region = fitz.Rect(cap_rect) if cap_rect else fitz.Rect(current_cap)
        if selected:
            region |= fitz.Rect(
                min(rect.x0 for rect in selected),
                min(rect.y0 for rect in selected),
                max(rect.x1 for rect in selected),
                max(rect.y1 for rect in selected),
            )
        region = (region + (-pad, -pad, pad, pad)) & page.rect

        pix = page.get_pixmap(matrix=fitz.Matrix(dpi_scale, dpi_scale), clip=region)
        image = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
        if image.width > max_width:
            image = image.resize((max_width, round(image.height * max_width / image.width)), Image.LANCZOS)

        rasters = [rect for rect in raster_rects(page) if rect.y0 >= region.y0 - 2 and rect.y1 <= region.y1 + 2]
        raster_area = sum(max(0.0, rect.width) * max(0.0, rect.height) for rect in rasters)
        is_photo = (raster_area / max(1.0, region.width * region.height)) > 0.45
        filename = f"fig{fig_num}.jpg" if is_photo else f"fig{fig_num}.png"
        if is_photo:
            image.save(out_dir / filename, format="JPEG", quality=84, optimize=True)
        else:
            image.save(out_dir / filename, format="PNG", optimize=True)
        manifest.append(
            {
                "num": fig_num,
                "page": page_number + 1,
                "file": filename,
                "path": (out_dir / filename).as_posix(),
                "layout": layout,
                "caption": cap_text,
            }
        )

    with (out_dir / "figures_manifest.json").open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return manifest
