# AP-01 synthetic Posteingang fixtures

No real client data. Dataset version 1.0; contract schema version 1.0.

- `manifest.json`: file hashes, split, permitted Customer scope, expected outcomes.
- `sources.json`, `texts/`, `annotations/`: PDF source and independent semantic ground truth. Never provider/OCR input.
- `pdfs/`: 16 synthetic originals; AP01-11 is image-only, AP01-12 has three pages.
- `ocr/`, `extraction/`, `reviews/`: deterministic operational expectations, not model quality measurements.
- `catalog.json`: synthetic server-side Customers/users; never sent to the model.
- `scenarios.json`: declarative specifications for later ERP runtime tests.

12 development / 4 holdout. Source variants AP01-01/AP01-16 stay in development. Holdout must not be used to tune prompts. AP01-11 has an actual action/deadline in semantic annotations even though the expected fallback extraction is unreadable.

Install `scripts/requirements-posteingang.txt` in a separate environment, then run `python scripts/validate_posteingang_fixtures.py` and `python -m unittest discover -s tests/posteingang -p 'test_*.py' -v` from the repository root. The PDF generator changes only PDFs; it never repairs gold or manifest hashes automatically.

Full contracts, reproduction instructions and pilot criteria: [contracts v1.0](../../../../docs/plans/ai-posteingang/contracts.md).
