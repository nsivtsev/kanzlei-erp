"""Offline, CPU-only PP-OCRv5 Mobile Latin with Docling table structure."""

from __future__ import annotations

from functools import cached_property
from hashlib import file_digest
from io import BytesIO
from pathlib import Path

from kanzlei_ai.documents.parse import (
    MAX_PAGES,
    MAX_PARSE_SECONDS,
    MAX_SOURCE_BYTES,
    Block,
    RetryableExtractionError,
    TerminalExtractionError,
    document_blocks,
    validate_pdf_input,
)

PADDLE_OCR_PROFILE = "paddle-ppocrv5-mobile-latin-v1"
# RapidAI/RapidOCR v3.10.0 ONNX weights, with the Latin alphabet embedded in Rec.
_PINNED_MODELS = {
    "det": (
        "ch_PP-OCRv5_det_mobile.onnx",
        "4d97c44a20d30a81aad087d6a396b08f786c4635742afc391f6621f5c6ae78ae",
    ),
    "cls": (
        "ch_PP-LCNet_x0_25_textline_ori_cls_mobile.onnx",
        "54379ae5174d026780215fc748a7f31910dee36818e63d49e17dc598ecc82df7",
    ),
    "rec": (
        "latin_PP-OCRv5_rec_mobile.onnx",
        "b20bd37c168a570f583afbc8cd7925603890efbcdc000a59e22c269d160b5f5a",
    ),
}
_HERON = "docling-project--docling-layout-heron"
_TABLEFORMER = "docling-project--docling-models/model_artifacts/tableformer/accurate"
_DOCLING_ASSETS = {
    # docling-project/docling-layout-heron @ 8f39ad3c0b4c58e9c2d2c84a38465abf757272d8
    f"{_HERON}/model.safetensors": (
        "00333a43451945aaf89db8ca9c0a17e75d1537c17db60fdb91aa95f4c7929e0c"
    ),
    f"{_HERON}/config.json": "fdea30805ce2f5666b147fca941dcdd27ad468e27d6ed21902207d3da056a97d",
    f"{_HERON}/preprocessor_config.json": (
        "cd38cd59999e7a95d68e487fbe5132df3d4e5c32a0836add57e6126ba0c4eaf1"
    ),
    # docling-project/docling-models @ fc0f2d45e2218ea24bce5045f58a389aed16dc23
    f"{_TABLEFORMER}/tableformer_accurate.safetensors": (
        "2a7d6c924b3cd12fb99a09280ca9c33a89c5d60b93253617d2e088c1a40374d9"
    ),
    f"{_TABLEFORMER}/tm_config.json": (
        "984e122ceb8ccf84d84c9d2882f6f2302a44b4f1e577babd6289892c36f3cffd"
    ),
}


def _verify_asset(path: Path, expected_sha256: str) -> None:
    with path.open("rb") as weights:
        if file_digest(weights, "sha256").hexdigest() != expected_sha256:
            raise RetryableExtractionError()


class PaddleOcrProvider:
    """Reuse local Docling/RapidOCR models within a worker process."""

    profile = PADDLE_OCR_PROFILE

    def __init__(self, model_dir: Path, *, artifacts_path: Path):
        self.model_dir = model_dir.expanduser().resolve()
        self.artifacts_path = artifacts_path.expanduser().resolve()

    @cached_property
    def _converter(self):
        from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import (
            LayoutObjectDetectionOptions,
            OcrMode,
            PdfPipelineOptions,
            RapidOcrOptions,
            TableFormerMode,
            TableStructureOptions,
        )
        from docling.document_converter import DocumentConverter, PdfFormatOption
        from rapidocr import LangRec, ModelType, OCRVersion

        model_paths = {}
        for role, (filename, expected_sha256) in _PINNED_MODELS.items():
            path = self.model_dir / filename
            _verify_asset(path, expected_sha256)
            model_paths[f"{role}_model_path"] = str(path)
        for filename, expected_sha256 in _DOCLING_ASSETS.items():
            _verify_asset(self.artifacts_path / filename, expected_sha256)

        options = PdfPipelineOptions(
            artifacts_path=self.artifacts_path,
            accelerator_options=AcceleratorOptions(device=AcceleratorDevice.CPU, num_threads=2),
            document_timeout=MAX_PARSE_SECONDS,
            enable_remote_services=False,
            do_ocr=True,
            do_table_structure=True,
            force_backend_text=False,
            layout_options=LayoutObjectDetectionOptions.from_preset("layout_heron_default"),
            table_structure_options=TableStructureOptions(mode=TableFormerMode.ACCURATE),
            ocr_options=RapidOcrOptions(
                backend="onnxruntime",
                lang=["latin"],
                mode=OcrMode.FULL_PAGE,
                rapidocr_params={
                    "Det.ocr_version": OCRVersion.PPOCRV5,
                    "Cls.ocr_version": OCRVersion.PPOCRV5,
                    "Rec.ocr_version": OCRVersion.PPOCRV5,
                    "Det.model_type": ModelType.MOBILE,
                    "Cls.model_type": ModelType.MOBILE,
                    "Rec.model_type": ModelType.MOBILE,
                    "Rec.lang_type": LangRec.LATIN,
                },
                **model_paths,
            ),
        )
        return DocumentConverter(
            allowed_formats=[InputFormat.PDF],
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)},
        )

    def recognize_page(self, content: bytes, page: int) -> list[Block]:
        page_count = validate_pdf_input(content)
        if type(page) is not int or not 1 <= page <= page_count:
            raise TerminalExtractionError("INVALID_PDF_PAGE")
        try:
            from docling.datamodel.base_models import DocumentStream
            from docling.datamodel.document import ConversionStatus

            # Docling rasterizes only the selected PDF page, runs RapidOCR on the
            # full image and uses the recognized boxes for TableFormer matching.
            result = self._converter.convert(
                DocumentStream(name="source.pdf", stream=BytesIO(content)),
                page_range=(page, page),
                max_num_pages=MAX_PAGES,
                max_file_size=MAX_SOURCE_BYTES,
            )
            if result.status is not ConversionStatus.SUCCESS or set(result.document.pages) != {
                page
            }:
                raise RetryableExtractionError()
            return [block for block in document_blocks(result.document) if block.page == page]
        except Exception:
            raise RetryableExtractionError() from None
