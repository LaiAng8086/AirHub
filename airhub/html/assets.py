"""Inline local image assets into an HTML file.

This helper intentionally performs no fetching or downloading. It only rewrites
local image paths that the producer already prepared.
"""

from __future__ import annotations

import argparse
import base64
import io
import mimetypes
import re
from pathlib import Path

from airhub.paths import PROJECT_ROOT


ABSOLUTE_RE = re.compile(r"^(https?:|data:|//)", re.IGNORECASE)
SRC_RE = re.compile(r"src=[\"']([^\"']+)[\"']")


def image_to_data_uri(path: Path, quality: int = 84, max_width: int = 980) -> str:
    try:
        from PIL import Image

        image = Image.open(path).convert("RGB")
        if image.width > max_width:
            image = image.resize(
                (max_width, round(image.height * max_width / image.width)),
                Image.LANCZOS,
            )
        buf = io.BytesIO()
        if path.suffix.lower() == ".png":
            image.save(buf, format="PNG", optimize=True)
            mime = "image/png"
        else:
            image.save(buf, format="JPEG", quality=quality, optimize=True)
            mime = "image/jpeg"
        payload = buf.getvalue()
    except Exception:
        payload = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return f"data:{mime};base64,{base64.b64encode(payload).decode()}"


def _resolve_image_path(src: str, html_path: Path, base_dir: Path) -> Path | None:
    raw = Path(src)
    candidates = [raw] if raw.is_absolute() else [base_dir / raw, html_path.parent / raw, PROJECT_ROOT / raw]
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file():
            return resolved
    return None


def embed_local_images(
    html_path: Path,
    output_path: Path | None = None,
    base_dir: Path | None = None,
    quality: int = 84,
    max_width: int = 980,
) -> dict[str, int | str]:
    html = html_path.read_text(encoding="utf-8")
    resolved_base = base_dir or html_path.parent
    stats = {"embedded": 0, "skipped": 0, "missing": 0}

    def replace(match: re.Match[str]) -> str:
        src = match.group(1)
        if ABSOLUTE_RE.match(src):
            stats["skipped"] += 1
            return match.group(0)
        path = _resolve_image_path(src, html_path, resolved_base)
        if path is None:
            stats["missing"] += 1
            return match.group(0)
        stats["embedded"] += 1
        return f'src="{image_to_data_uri(path, quality=quality, max_width=max_width)}"'

    output = SRC_RE.sub(replace, html)
    target = output_path or html_path
    target.write_text(output, encoding="utf-8")
    stats["output"] = target.as_posix()
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Inline local images in a skill-generated HTML file")
    parser.add_argument("html")
    parser.add_argument("out", nargs="?")
    parser.add_argument("--base-dir")
    parser.add_argument("--quality", type=int, default=84)
    parser.add_argument("--max-width", type=int, default=980)
    args = parser.parse_args()
    stats = embed_local_images(
        Path(args.html),
        Path(args.out) if args.out else None,
        Path(args.base_dir) if args.base_dir else None,
        quality=args.quality,
        max_width=args.max_width,
    )
    print(f"[DONE] embedded assets {stats}")


if __name__ == "__main__":
    main()
