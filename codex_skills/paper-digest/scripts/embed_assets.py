#!/usr/bin/env python3
"""
embed_assets.py — Inline every locally-referenced <img> into an HTML file as a
base64 data URI, producing ONE self-contained file.

Why this exists: when the digest is delivered via present_files, the chat
preview renders the HTML in isolation, so relative paths like
src="assets/fig1.png" do NOT load and the user sees broken images. Inlining the
bytes as data URIs fixes this. ArXiv and PDF images must both have been localized
by Producer before this script runs; a downloadable digest must not rely on hotlinks.

Usage:
    python3 embed_assets.py <input.html> [output.html] [--base-dir DIR] [--quality 84] [--max-width 980]

- Rewrites src="assets/..." (and any other relative local image path) to a
  data: URI. Already-absolute (http/https/data:) srcs are left untouched.
- Recompresses on the fly as a safety net (PNG kept as PNG; everything else
  JPEG) so the final HTML stays small even if upstream files were large.

Requires: pillow  ->  pip install pillow --break-system-packages
"""
import sys, os, re, io, base64, argparse, mimetypes

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("html")
    ap.add_argument("out", nargs="?", default=None)
    ap.add_argument("--base-dir", default=None,
                    help="directory that relative src paths are resolved against "
                         "(default: the HTML file's own directory)")
    ap.add_argument("--quality", type=int, default=84)
    ap.add_argument("--max-width", type=int, default=980)
    args = ap.parse_args()

    from PIL import Image

    html = open(args.html, encoding="utf-8").read()
    base_dir = args.base_dir or os.getcwd()
    out_path = args.out or args.html

    n_done = [0]; n_skip = [0]; n_miss = [0]

    def to_data_uri(path):
        ext = os.path.splitext(path)[1].lower()
        im = Image.open(path).convert("RGB")
        if im.width > args.max_width:
            im = im.resize((args.max_width, round(im.height * args.max_width / im.width)),
                           Image.LANCZOS)
        buf = io.BytesIO()
        if ext == ".png":
            im.save(buf, format="PNG", optimize=True); mime = "image/png"
        else:
            im.save(buf, format="JPEG", quality=args.quality, optimize=True); mime = "image/jpeg"
        return f"data:{mime};base64,{base64.b64encode(buf.getvalue()).decode()}"

    def repl(m):
        src = m.group(1)
        if re.match(r'(https?:|data:)', src, re.I):
            n_skip[0] += 1
            return m.group(0)                       # leave absolute/data URIs alone
        path = src if os.path.isabs(src) else os.path.join(base_dir, src)
        if not os.path.exists(path):
            n_miss[0] += 1
            print(f"  WARNING missing image: {src}", file=sys.stderr)
            return m.group(0)
        uri = to_data_uri(path)
        n_done[0] += 1
        return f'src="{uri}"'

    html = re.sub(r'src="([^"]+)"', repl, html)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    size_mb = os.path.getsize(out_path) / 1024 / 1024
    print(f"Embedded {n_done[0]} image(s); skipped {n_skip[0]} absolute; missing {n_miss[0]}.")
    print(f"Wrote {out_path} ({size_mb:.1f} MB)")
    if n_miss[0]:
        print("Some images were missing — fix paths and re-run.", file=sys.stderr)
        raise SystemExit(2)

if __name__ == "__main__":
    main()
