"""Real isolated parser; no site or OCR needed."""

import io
import unittest
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfWriter


class TestIntakePDF(unittest.TestCase):
	def validator(self):
		from kanzlei_erp.posteingang_pdf import validate_pdf

		return validate_pdf

	def pdf(self, pages=1, encrypted=False):
		writer = PdfWriter()
		for _ in range(pages):
			writer.add_blank_page(width=200, height=300)
		if encrypted:
			writer.encrypt("")
		out = io.BytesIO()
		writer.write(out)
		return out.getvalue()

	def test_valid_pdf_and_raster_scan(self):
		self.assertEqual(self.validator()(self.pdf(), 50), 1)
		scan = Path(__file__).resolve().parents[2] / "kanzlei_erp/tests/fixtures/posteingang/pdfs/AP01-11.pdf"
		self.assertEqual(self.validator()(scan.read_bytes(), 50), 1)

	def test_invalid_encrypted_zero_pages_and_page_limit(self):
		for data, limit, code in (
			(b"not PDF", 50, "PDF_INVALID"),
			(self.pdf(encrypted=True), 50, "PDF_ENCRYPTED"),
			(self.pdf(0), 50, "PDF_INVALID"),
			(self.pdf(2), 1, "PAGE_LIMIT_EXCEEDED"),
		):
			with self.subTest(code=code), self.assertRaisesRegex(ValueError, code):
				self.validator()(data, limit)

	def test_timeout_is_safe(self):
		import subprocess

		validate = self.validator()
		with patch(
			"kanzlei_erp.posteingang_pdf.subprocess.run", side_effect=subprocess.TimeoutExpired("parser", 10)
		):
			with self.assertRaisesRegex(ValueError, "PDF_VALIDATION_LIMIT"):
				validate(self.pdf(), 50)

	def test_fifty_pages_allowed_and_fifty_one_rejected(self):
		self.assertEqual(self.validator()(self.pdf(50), 50), 50)
		with self.assertRaisesRegex(ValueError, "PAGE_LIMIT_EXCEEDED"):
			self.validator()(self.pdf(51), 50)
