from __future__ import annotations

import tempfile
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


class PdfFigureExtractionTest(unittest.TestCase):
    def test_captions_without_punctuation_and_multiline_titles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code = textwrap.dedent(
                """
                import sys
                from pathlib import Path

                # Match production import order in an isolated process so a
                # different test cannot preload PyMuPDF's system libstdc++.
                from PIL import Image  # noqa: F401
                import fitz

                from airhub.media.pdf_figures import extract_pdf_figures

                root = Path(sys.argv[1])
                pdf_path = root / "paper.pdf"
                output_dir = root / "figures"
                doc = fitz.open()

                first = doc.new_page()
                first.draw_rect(fitz.Rect(90, 60, 500, 180), color=(0, 0, 0), fill=(0.9, 0.9, 0.9))
                first.insert_text((70, 220), "Figure 1 Overview without punctuation", fontsize=9)

                second = doc.new_page()
                second.draw_rect(fitz.Rect(90, 60, 500, 180), color=(0, 0, 0), fill=(0.8, 0.8, 0.8))
                second.insert_textbox(
                    fitz.Rect(70, 205, 500, 250),
                    "Figure 2\\n3D Scene-to-Token Adapter.",
                    fontsize=9,
                )
                doc.save(pdf_path)
                doc.close()

                manifest = extract_pdf_figures(pdf_path, output_dir)
                assert [item["num"] for item in manifest] == [1, 2]
                assert "Figure 1 Overview" in manifest[0]["caption"]
                assert "Figure 2 3D Scene-to-Token" in manifest[1]["caption"]
                assert all(Path(item["path"]).is_file() for item in manifest)
                """
            )
            result = subprocess.run(
                [sys.executable, "-c", code, str(root)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
