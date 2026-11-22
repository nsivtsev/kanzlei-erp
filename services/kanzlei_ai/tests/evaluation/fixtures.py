"""Build synthetic German PDFs with native text and realistic raster-only scans."""

from io import BytesIO
from pathlib import Path

INVOICE_ID = "RE-2026-0714"
INVOICE_DATE = "31.03.2026"
TABLE = [
    ("Leistung", "Menge", "Betrag"),
    ("Buchführung", "3", "390,00 EUR"),
    ("Beratung", "5", "859,50 EUR"),
]


def _scan_font():
    from PIL import ImageFont

    for name in (
        "DejaVuSans.ttf",
        "LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ):
        try:
            return ImageFont.truetype(name, size=52), ImageFont.truetype(name, size=36)
        except OSError:
            continue
    raise OSError("Install DejaVu Sans or Liberation Sans to render the German scans")


def _native_invoice(writer):
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    page = writer.add_blank_page(width=595, height=842)
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {
                    NameObject("/F1"): DictionaryObject(
                        {
                            NameObject("/Type"): NameObject("/Font"),
                            NameObject("/Subtype"): NameObject("/Type1"),
                            NameObject("/BaseFont"): NameObject("/Helvetica"),
                            NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
                        }
                    )
                }
            )
        }
    )
    stream = DecodedStreamObject()
    stream.set_data(
        (
            f"BT /F1 24 Tf 50 770 Td (Rechnung {INVOICE_ID}) Tj\n"
            f"/F1 14 Tf 0 -50 Td (Rechnungsdatum: {INVOICE_DATE}) Tj\n"
            "0 -30 Td (Kunde: Fiktive Aktenwerk GmbH) Tj\n"
            "0 -30 Td (Die Leistungsübersicht folgt auf Seite 2.) Tj ET"
        ).encode("cp1252")
    )
    page.replace_contents(stream)


def _scanned_page(title, lines, *, table=()):
    from PIL import Image, ImageDraw
    from pypdf import PdfReader

    image = Image.new("RGB", (1240, 1754), "#f5f4ed")
    draw = ImageDraw.Draw(image)
    heading, body = _scan_font()
    draw.text((100, 100), title, fill="#252525", font=heading)
    y = 225
    for line in lines:
        draw.text((100, y), line, fill="#252525", font=body)
        y += 65
    if table:
        x_columns = (100, 560, 775, 1140)
        y += 35
        bottom = y + 85 * len(table)
        for x in x_columns:
            draw.line((x, y, x, bottom), fill="#555555", width=2)
        for row in table:
            draw.line((100, y, 1140, y), fill="#555555", width=2)
            for x, text in zip(x_columns[:-1], row, strict=True):
                draw.text((x + 20, y + 18), text, fill="#252525", font=body)
            y += 85
        draw.line((100, bottom, 1140, bottom), fill="#555555", width=2)
    draw.text((100, 1620), "Synthetischer Beleg für OCR-Tests", fill="#4b4b4b", font=body)
    image = image.rotate(0.7, resample=Image.Resampling.BICUBIC, fillcolor="#f5f4ed")
    buffer = BytesIO()
    image.save(buffer, format="PDF", resolution=150, quality=88)
    buffer.seek(0)
    return PdfReader(buffer).pages[0]


def write_fixtures(directory: Path) -> None:
    from pypdf import PdfWriter

    directory.mkdir(parents=True, exist_ok=True)
    invoice = PdfWriter()
    _native_invoice(invoice)
    invoice.add_page(
        _scanned_page(
            "Leistungsübersicht",
            [f"Rechnung {INVOICE_ID}", "Gesamtbetrag: 1.249,50 EUR"],
            table=TABLE,
        )
    )
    invoice.write(directory / "rechnung-misch.pdf")
    correction = PdfWriter()
    correction.add_page(
        _scanned_page(
            "Abweichende Zweitschrift",
            [
                f"Rechnung {INVOICE_ID}",
                f"Rechnungsdatum: {INVOICE_DATE}",
                "Gesamtbetrag: 1.429,50 EUR",
            ],
        )
    )
    correction.write(directory / "korrektur-scan.pdf")
