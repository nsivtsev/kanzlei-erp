import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent
CLI = ROOT / "evaluate.py"
GOLD = ROOT / "gold.json"


def invoke(*args):
    return subprocess.run(
        [sys.executable, str(CLI), *map(str, args)], capture_output=True, text=True, check=False
    )


def test_corpus_contains_native_and_raster_only_pages_and_german_table_rows(tmp_path):
    pypdf = pytest.importorskip("pypdf")
    pytest.importorskip("PIL")
    result = invoke("fixtures", tmp_path)
    assert result.returncode == 0, result.stderr
    mixed = pypdf.PdfReader(tmp_path / "rechnung-misch.pdf")
    assert len(mixed.pages) == 2
    assert "RE-2026-0714" in mixed.pages[0].extract_text()
    assert not mixed.pages[1].extract_text().strip()
    assert len(mixed.pages[1].images) == 1
    correction = pypdf.PdfReader(tmp_path / "korrektur-scan.pdf")
    assert not correction.pages[0].extract_text().strip()
    assert len(correction.pages[0].images) == 1


def test_scores_retrieval_answer_citations_and_ocr_independently(tmp_path):
    observations = {
        "questions": {
            "identifier": {
                "retrieved": [{"document": "rechnung-misch.pdf", "page": 1}],
                "outcome": "answered",
                "answer": "Die Rechnungsnummer lautet RE-2026-07140.",
                "citations": [{"document": "rechnung-misch.pdf", "page": 1}],
            },
            "table": {
                "retrieved": [{"document": "rechnung-misch.pdf", "page": 1}],
                "outcome": "answered",
                "answer": "Buchführung | 3 | 390,00 EUR",
                "citations": [{"document": "rechnung-misch.pdf", "page": 1}],
            },
            "contradiction": {
                "retrieved": [
                    {"document": "rechnung-misch.pdf", "page": 2},
                    {"document": "korrektur-scan.pdf", "page": 1},
                ],
                "outcome": "contradiction",
                "answer": "Die Belege widersprechen sich bei 1.249,50 EUR und 1.429,50 EUR.",
                "citations": [{"document": "rechnung-misch.pdf", "page": 2}],
            },
            "unanswerable": {
                "retrieved": [],
                "outcome": "unanswerable",
                "answer": "",
                "citations": [],
            },
        },
        "pages": {
            "rechnung-misch.pdf": {"2": "Leistung | Menge | Betrag\nBuchführung | 3 | 390,00 EUR"}
        },
    }
    path = tmp_path / "observations.json"
    path.write_text(json.dumps(observations))
    result = invoke("score", path)
    assert result.returncode == 1, result.stderr
    report = json.loads(result.stdout)
    assert report["questions"]["identifier"] == {
        "retrieval": True,
        "answer": False,
        "citations": True,
    }
    assert report["questions"]["table"] == {"retrieval": False, "answer": True, "citations": False}
    assert report["questions"]["contradiction"] == {
        "retrieval": True,
        "answer": True,
        "citations": False,
    }
    assert report["questions"]["unanswerable"] == {
        "retrieval": None,
        "answer": True,
        "citations": True,
    }
    assert report["questions"]["date"]["answer"] is False
    assert report["ocr"]["rechnung-misch.pdf:2"]["rows"]["Buchführung"] is True
    assert report["ocr"]["rechnung-misch.pdf:2"]["rows"]["Beratung"] is False


def test_complete_evidence_and_exact_table_cells_pass(tmp_path):
    gold = json.loads(GOLD.read_text())
    observations = {
        "questions": {
            question["id"]: {
                "retrieved": question["evidence"],
                "outcome": question["outcome"],
                "answer": question["example_answer"],
                "citations": question["evidence"],
            }
            for question in gold["questions"]
        },
        "pages": {
            "rechnung-misch.pdf": {
                "2": "Gesamtbetrag: 1.249,50 EUR\n| Leistung | Menge | Betrag |\n"
                "| --- | --- | --- |\n| Buchführung | 3 | 390,00 EUR |\n"
                "| Beratung | 5 | 859,50 EUR |",
            },
            "korrektur-scan.pdf": {"1": "RE-2026-0714\n31.03.2026\nGesamtbetrag: 1.429,50 EUR"},
        },
    }
    path = tmp_path / "observations.json"
    path.write_text(json.dumps(observations))
    result = invoke("score", path)
    assert result.returncode == 0, result.stdout + result.stderr
    observations["pages"]["rechnung-misch.pdf"]["2"] = (
        "Gesamtbetrag: 1.249,50 EUR\n| Buchführung | 5 | 390,00 EUR |\n"
        "| Beratung | 3 | 859,50 EUR |"
    )
    path.write_text(json.dumps(observations))
    result = invoke("score", path)
    assert result.returncode == 1
    rows = json.loads(result.stdout)["ocr"]["rechnung-misch.pdf:2"]["rows"]
    assert rows == {"Buchführung": False, "Beratung": False}


def test_unanswerable_outcome_requires_an_actual_abstention(tmp_path):
    observations = {
        "pages": {},
        "questions": {
            "unanswerable": {
                "outcome": "unanswerable",
                "answer": "Die Rechnung wurde am 14.04.2026 bezahlt.",
                "citations": [],
            },
        },
    }
    path = tmp_path / "fabricated-answer.json"
    path.write_text(json.dumps(observations), encoding="utf-8")
    result = invoke("score", path)
    assert result.returncode == 1
    assert not json.loads(result.stdout)["questions"]["unanswerable"]["answer"]
