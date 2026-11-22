# Synthetic German OCR and answer evaluation

From `services/kanzlei_ai`, generate the corpus in a temporary directory with
the document environment. The scan generator uses Pillow, pypdf, and Helvetica
on macOS or DejaVu Sans or Liberation Sans on Linux.

```sh
uv run --extra documents python tests/evaluation/evaluate.py fixtures /tmp/task8-fixtures
```

`rechnung-misch.pdf` has a native invoice cover and a raster-only table.
`korrektur-scan.pdf` has a raster-only copy with a conflicting total.
`gold.json` contains the exact queries, expected facts, and page references.
Use only these synthetic files. Run the pipeline under evaluation and capture
its observations in this JSON format.

```json
{
  "pages": {
    "rechnung-misch.pdf": {
      "2": "Gesamtbetrag: 1.249,50 EUR\n| Leistung | Menge | Betrag |\n| Buchführung | 3 | 390,00 EUR |\n| Beratung | 5 | 859,50 EUR |"
    }
  },
  "questions": {
    "identifier": {
      "retrieved": [{"document": "rechnung-misch.pdf", "page": 1}],
      "outcome": "answered",
      "answer": "Die Rechnungsnummer lautet RE-2026-0714.",
      "citations": [{"document": "rechnung-misch.pdf", "page": 1}]
    }
  }
}
```

Key `pages` by fixture file and one-based PDF page, using page text with
pipe-delimited table rows. Key `questions` by the question IDs from `gold.json`.
Record retrieved passages in rank order, preserving their fixture filename and
one-based page. Transcribe the answer and its cited pages. Assign the outcome
based on the answer's content. Use `contradiction` only if the answer
acknowledges the conflict. Use `unanswerable` when it abstains because the
documents lack the requested fact, with an empty answer and no citations.

```sh
python tests/evaluation/evaluate.py score /tmp/observations.json --top-k 5
```

The report scores OCR facts and table cells, retrieval, answer facts and
outcomes, and exact citation pages. Retrieval passes when the first K passages
cover every gold evidence page. Missing cases count as failures. The CLI scores
recorded observations locally and returns 0 when every check passes, 1 for a
failed check, and 2 for unreadable input.
