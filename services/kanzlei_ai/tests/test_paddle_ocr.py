"""Local OCR contracts using synthetic PDFs and an isolated conversion seam."""

import base64
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from kanzlei_ai.documents.parse import Block, ExtractionError, TerminalExtractionError

FIXTURES = Path(__file__).parent / "fixtures" / "documents"


@pytest.fixture
def content():
    return base64.b64decode((FIXTURES / "synthetic-german-native.pdf.b64").read_text())


def test_provider_pins_only_the_requested_page_and_preserves_table_cells(
    content, tmp_path, monkeypatch
):
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.document import ConversionStatus
    from docling.document_converter import DocumentConverter

    from kanzlei_ai.providers import paddle_ocr
    from kanzlei_ai.providers.ocr import OcrProvider

    weights = {
        role: (filename, hashlib.sha256(role.encode()).hexdigest())
        for role, (filename, _) in paddle_ocr._PINNED_MODELS.items()
    }
    monkeypatch.setattr(paddle_ocr, "_PINNED_MODELS", weights)
    for role, (filename, _) in weights.items():
        (tmp_path / filename).write_bytes(role.encode())

    # Reject missing layout and TableFormer checkpoints before conversion.
    provider: OcrProvider = paddle_ocr.PaddleOcrProvider(tmp_path, artifacts_path=tmp_path)
    with pytest.raises(ExtractionError, match="^EXTRACTION_UNAVAILABLE$"):
        provider.recognize_page(content, 2)

    layout_weights = {
        filename: hashlib.sha256(filename.encode()).hexdigest()
        for filename in paddle_ocr._DOCLING_ASSETS
    }
    monkeypatch.setattr(paddle_ocr, "_DOCLING_ASSETS", layout_weights)
    for filename in layout_weights:
        path = tmp_path / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(filename.encode())

    table_markdown = "| Leistung | Betrag |\n| --- | --- |\n| Beratung | 42 EUR |"

    class TableItem:
        prov = [SimpleNamespace(page_no=2)]

        def export_to_markdown(self, document):
            return table_markdown

    class Document:
        pages = {2: object()}

        def iterate_items(self):
            yield TableItem(), 0

    def convert(self, stream, *, page_range, **_kwargs):
        assert stream.stream.read() == content
        assert page_range == (2, 2)
        options = self.format_to_options[InputFormat.PDF].pipeline_options
        assert options.artifacts_path == tmp_path
        assert options.enable_remote_services is False
        assert options.do_table_structure and options.do_ocr
        assert options.accelerator_options.device == "cpu"
        ocr = options.ocr_options
        assert ocr.backend == "onnxruntime"
        assert ocr.mode.value == "full_page"
        assert ocr.lang == ["latin"]
        for role, (filename, _) in weights.items():
            assert getattr(ocr, f"{role}_model_path") == str(tmp_path / filename)
        return SimpleNamespace(status=ConversionStatus.SUCCESS, document=Document())

    monkeypatch.setattr(DocumentConverter, "convert", convert)
    assert provider.recognize_page(content, 2) == [Block(2, "table", table_markdown)]


@pytest.mark.parametrize("page", [0, 4])
def test_invalid_page_is_terminal_even_without_local_weights(content, tmp_path, page):
    from kanzlei_ai.providers.paddle_ocr import PaddleOcrProvider

    provider = PaddleOcrProvider(tmp_path, artifacts_path=tmp_path)
    with pytest.raises(TerminalExtractionError, match="INVALID_PDF_PAGE"):
        provider.recognize_page(content, page)


@pytest.mark.parametrize("corrupted", [False, True])
def test_missing_or_corrupt_weights_fail_safely(content, tmp_path, corrupted):
    from kanzlei_ai.providers.paddle_ocr import _PINNED_MODELS, PaddleOcrProvider

    if corrupted:
        for filename, _ in _PINNED_MODELS.values():
            (tmp_path / filename).write_bytes(b"offline model substitute")

    provider = PaddleOcrProvider(tmp_path, artifacts_path=tmp_path)
    with pytest.raises(ExtractionError, match="^EXTRACTION_UNAVAILABLE$") as exc:
        provider.recognize_page(content, 1)
    assert exc.value.retryable
