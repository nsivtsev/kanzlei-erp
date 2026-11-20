# OCR archive bootstrap and Mistral cutover

From the service directory, use Python 3.13 and `uv sync --locked --extra paddle`
on the Mac. Provision the public [RapidOCR PP-OCRv5 v3.10.0 ONNX weights](https://www.modelscope.cn/models/RapidAI/RapidOCR/tree/v3.10.0/onnx/PP-OCRv5)
in `AI_PADDLE_MODEL_DIR`. The worker verifies the three model filenames and
SHA-256 values defined in `kanzlei_ai.providers.paddle_ocr`. Provision the Docling
assets below in `AI_PADDLE_DOCLING_ARTIFACTS_DIR` and set `DOCLING_ARTIFACTS_PATH`
to the same absolute directory. Stage the weights on a machine with network
access, then copy them read-only to the worker.

| Model repository | Revision | Files |
| --- | --- | --- |
| `docling-project/docling-layout-heron` | `8f39ad3c0b4c58e9c2d2c84a38465abf757272d8` | `config.json`, `preprocessor_config.json`, `model.safetensors` |
| `docling-project/docling-models` | `fc0f2d45e2218ea24bce5045f58a389aed16dc23` | `model_artifacts/tableformer/accurate/*` |

Use `huggingface_hub.snapshot_download` with the listed revisions and files.
The worker expects the respective `local_dir` names `docling-project--docling-layout-heron`
and `docling-project--docling-models` inside the Docling asset directory.
Set `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` for the Mac worker.

## Primary archive pass

1. Run `uv run --locked python -m kanzlei_ai.coordinator migrate` against the
   Kanzlei AI database. Configure the approved clients on the ERP ingestion
   connector. Give the Mac worker access to this connector, the private artifact
   store, the Kanzlei AI database and Redis via the private network. Keep broker
   and database ports private. The Mac does not need the ERP browser key or a
   Mistral key.
2. Set `AI_INGESTION_ENABLED=true`, `AI_INGESTION_OCR_PROFILE=paddle`,
   `AI_SEARCH_INDEX_ENABLED=false`, and `AI_CHAT_ENABLED=false` for archive
   bootstrap. Keep `AI_MODEL_PROFILE=synthetic` and `AI_MISTRAL_ENABLED=false`.
   Start the Mac worker, start an inventory with `python -m kanzlei_ai.coordinator scan`,
   and run the coordinator on the server.

   ```sh
   uv run --locked --extra paddle celery -A kanzlei_ai.worker:app worker \
     --hostname=paddle@%h --loglevel=INFO --concurrency=1 --queues=ocr-paddle
   ```

3. Once inventory finishes, consume the change feed to its current end and
   pause ingestion polling. Leave dispatch and recovery running while the OCR
   queue drains. Stop the coordinator's `run` process and use its `recover` and
   `dispatch` commands until the queue settles. Keep ingestion enabled on the Mac
   worker. Review completed and failed jobs in PostgreSQL and verify that
   current successfully extracted sources have committed artifacts. Resolve or
   explicitly accept failed documents before cutover.

## Mistral cutover

1. Confirm approval for external processing for each pilot Mandant. Configure
   `kanzlei_ai_connector.external_processing` in the ERP's private site config
   with explicit per-Customer booleans for `ocr`, `embed` and `generate`. Verify
   that the approved Mistral endpoint exposes `mistral-ocr-4-1`, `mistral-embed`,
   and `mistral-small-2603`. Set `AI_MISTRAL_ENABLED=true`, `AI_MODEL_PROFILE=mistral`,
   the approved `AI_MISTRAL_ENDPOINT`, and the service-local `AI_MISTRAL_API_KEY`.
   Set `AI_SEARCH_INDEX_ENABLED=true` and keep chat off during backfill. Run a
   server worker consuming `celery,ocr-mistral`.
2. Run `python -m kanzlei_ai.coordinator cutover-ocr` for the configured
   connector. Resume polling from the saved feed cursor. New and changed
   documents select Mistral OCR for pages that need it. Current Paddle artifacts
   keep their provenance.
3. Run `python -m kanzlei_ai.coordinator reindex --missing-only` and repeat
   with `--cursor <next_cursor>` until there is no continuation. The index
   worker builds the separate 1024-dimensional Mistral collection directly from
   current artifacts. Check index job results and verify citations before setting
   `AI_CHAT_ENABLED=true`.
4. To stop external processing, revoke the affected Mandant policy immediately
   and stop the Mistral consumers and API before changing their model profile.
   Keep artifacts, leases, and cursors for recovery. Use `requeue-extract
   SOURCE_ID` to retry OCR for a specific current document. Run reindex again
   for artifacts whose embedding work needs to be retried.

## Synthetic results on the M1 Max

PaddleOCR passed all seven OCR checks on the German evaluation fixtures, including
amounts, the invoice identifier, the date, and the two exact table rows. In a
separate single-process benchmark on repeated synthetic scan pages, native PDF
parsing plus OCR took 16.6, 22.2 and 65.3 seconds for 1, 10 and 30 pages. The
one-page run included cold model loading. Peak resident memory was 2.5 GiB. This
benchmark excludes ERP, network and durable queue work.
