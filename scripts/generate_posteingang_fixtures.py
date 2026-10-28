#!/usr/bin/env python3
"""Render only synthetic PDFs. Never write gold answers or update their manifest hashes."""

import argparse
import json
import random
import textwrap
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "kanzlei_erp/tests/fixtures/posteingang"
PAGE = (595.2756, 841.8898)
MARK = "SYNTHETISCH / SYNTHETIC - KEINE REALEN MANDANTENDATEN"


def raster_page(text, page_number, page_count):
    image = Image.new("RGB", (1150, 1620), "#eeeeeb")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=22)
    draw.text((60, 50), MARK, font=ImageFont.load_default(size=24), fill="#852a2a")
    y = 140
    for line in text.splitlines():
        for wrapped in textwrap.wrap(line, width=78) or [""]:
            draw.text((65, y), wrapped, font=font, fill="#727270")
            y += 32
    draw.text((65, 1530), f"TESTSCAN - Seite {page_number}/{page_count}", font=font, fill="#727270")
    rng = random.Random(20261018 + page_number)
    # Intentionally poor scan, not an accidental rendering defect.
    for _ in range(18000):
        x, y = rng.randrange(image.width), rng.randrange(image.height)
        draw.point((x, y), fill=(rng.randrange(145, 235),) * 3)
    return image.resize((460, 648)).filter(ImageFilter.GaussianBlur(0.6)).resize((1150, 1620))


def render_case(case, output):
    target = output / "pdfs" / f"{case['case_id']}.pdf"
    target.parent.mkdir(parents=True, exist_ok=True)
    buffer = BytesIO()
    canvas = Canvas(buffer, pagesize=PAGE, invariant=1, pageCompression=1)
    canvas.setTitle(f"Synthetic Posteingang {case['case_id']}")
    canvas.setAuthor("Kanzlei ERP synthetic fixtures")
    canvas.setSubject("Artificial acceptance fixture; no real client data")
    for number, page in enumerate(case["pages"], 1):
        if case["generation"] == "raster":
            canvas.drawImage(ImageReader(raster_page(page, number, len(case["pages"]))), 0, 0, *PAGE)
        else:
            canvas.setFillColorRGB(0.55, 0.12, 0.12)
            canvas.setFont("Helvetica-Bold", 9)
            canvas.drawString(42, PAGE[1] - 40, MARK)
            canvas.setFillColorRGB(0.1, 0.13, 0.18)
            canvas.setFont("Helvetica-Bold", 17)
            canvas.drawString(42, PAGE[1] - 76, f"Posteingang - {case['case_id']}")
            canvas.setFont("Helvetica", 11)
            y = PAGE[1] - 115
            for line in page.splitlines():
                for wrapped in textwrap.wrap(line, width=83, break_long_words=False) or [""]:
                    if y < 65:
                        raise ValueError(f"{case['case_id']}: page text overflows; split the source page explicitly")
                    if canvas.stringWidth(wrapped, "Helvetica", 11) > PAGE[0] - 84:
                        raise ValueError(f"{case['case_id']}: source line is too wide")
                    canvas.drawString(42, y, wrapped)
                    y -= 17
            canvas.setFont("Helvetica", 9)
            canvas.setFillColorRGB(0.4, 0.43, 0.47)
            canvas.drawString(42, 36, f"Synthetisches Beispiel | 18.10.2026 | Seite {number}/{len(case['pages'])}")
        canvas.showPage()
    canvas.save()
    target.write_bytes(buffer.getvalue())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=FIXTURES)
    parser.add_argument("--sources", type=Path, default=FIXTURES / "sources.json")
    args = parser.parse_args()
    sources = json.loads(args.sources.read_text())
    if sources.get("synthetic") is not True:
        parser.error("only explicitly synthetic source specifications are accepted")
    for case in sources["cases"]:
        render_case(case, args.output)
    print(f"Rendered {len(sources['cases'])} synthetic PDFs; gold JSON and manifest unchanged")


if __name__ == "__main__":
    main()
