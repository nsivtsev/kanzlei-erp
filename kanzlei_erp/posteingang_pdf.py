"""Bounded structural PDF validation. The child never imports Frappe or performs OCR."""

import io
import json
import logging
import math
import subprocess
import sys
from pathlib import Path

MAX_BYTES = 20 * 1024 * 1024
ERRORS = {"PDF_INVALID", "PDF_ENCRYPTED", "PAGE_LIMIT_EXCEEDED", "PDF_VALIDATION_LIMIT"}


def validate_pdf(content, max_pages):
	try:
		result = subprocess.run(
			[sys.executable, "-I", str(Path(__file__).resolve()), str(max_pages)],
			input=content,
			stdout=subprocess.PIPE,
			stderr=subprocess.DEVNULL,
			timeout=10,
			check=False,
		)
	except subprocess.TimeoutExpired:
		raise ValueError("PDF_VALIDATION_LIMIT") from None
	if result.returncode or len(result.stdout) > 4096:
		raise ValueError("PDF_VALIDATION_LIMIT")
	try:
		value = json.loads(result.stdout)
		if value.get("error") in ERRORS:
			raise ValueError(value["error"])
		pages = value["pages"]
		if type(pages) is not int or not 1 <= pages <= max_pages:
			raise ValueError("PDF_INVALID")
		return pages
	except (KeyError, TypeError, json.JSONDecodeError):
		raise ValueError("PDF_INVALID") from None


def _inspect(content, max_pages):
	from pypdf import PdfReader

	if not content.startswith(b"%PDF-"):
		return {"error": "PDF_INVALID"}
	pdf = PdfReader(io.BytesIO(content), strict=True)
	if pdf.is_encrypted:
		return {"error": "PDF_ENCRYPTED"}
	count = 0
	for page in pdf.pages:
		count += 1
		if count > max_pages:
			return {"error": "PAGE_LIMIT_EXCEEDED"}
		width, height = float(page.mediabox.width), float(page.mediabox.height)
		if not all(math.isfinite(v) and v > 0 for v in (width, height)):
			return {"error": "PDF_INVALID"}
		stream = page.get_contents()
		if stream is not None:
			# Force lazy stream decoding and operator parsing inside the limits.
			_ = stream.operations
	return {"pages": count} if count else {"error": "PDF_INVALID"}


def _main():
	import resource

	resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
	resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
	resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
	resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
	logging.disable(logging.CRITICAL)
	try:
		content = sys.stdin.buffer.read(MAX_BYTES + 1)
		result = {"error": "PDF_INVALID"} if len(content) > MAX_BYTES else _inspect(content, int(sys.argv[1]))
	except MemoryError:
		result = {"error": "PDF_VALIDATION_LIMIT"}
	except Exception:
		result = {"error": "PDF_INVALID"}
	sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
	_main()
