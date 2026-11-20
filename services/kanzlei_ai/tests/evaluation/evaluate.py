"""Generate the synthetic corpus or score normalized local observations."""

import argparse
import json
import re
import unicodedata
from pathlib import Path

GOLD = Path(__file__).with_name("gold.json")


def normalize(text):
    return " ".join(unicodedata.normalize("NFC", text).split())


def contains(text, value):
    return bool(re.search(rf"(?<![\w-]){re.escape(value)}(?![\w-])", normalize(text)))


def page_refs(rows):
    refs = set()
    for row in rows:
        if (
            set(row) != {"document", "page"}
            or not isinstance(row["document"], str)
            or type(row["page"]) is not int
            or row["page"] < 1
        ):
            raise ValueError("Expected document name and one-based integer page")
        refs.add((row["document"], row["page"]))
    return refs


def score(gold, observed, *, top_k):
    questions = {}
    for question in gold["questions"]:
        actual = observed["questions"].get(question["id"], {})
        expected = page_refs(question["evidence"])
        answer = actual.get("answer", "")
        questions[question["id"]] = {
            "retrieval": expected <= page_refs(actual.get("retrieved", [])[:top_k])
            if expected
            else None,
            "answer": (
                actual.get("outcome") == question["outcome"]
                and (question["outcome"] != "unanswerable" or not answer.strip())
                and all(contains(answer, value) for value in question["required"])
                and not any(contains(answer, value) for value in question.get("forbidden", []))
            ),
            "citations": "citations" in actual and page_refs(actual["citations"]) == expected,
        }

    ocr = {}
    for source in gold["ocr"]:
        document, page = source["document"], str(source["page"])
        actual = observed["pages"].get(document, {}).get(page, "")
        lines = {
            tuple(normalize(cell) for cell in line.strip().strip("|").split("|"))
            for line in actual.splitlines()
            if "|" in line
        }
        result = {"facts": {value: contains(actual, value) for value in source["required"]}}
        if "headers" in source:
            result["headers"] = tuple(source["headers"]) in lines
            result["rows"] = {row[0]: tuple(row) in lines for row in source["rows"]}
        ocr[f"{document}:{page}"] = result

    metrics = {}
    for metric in ("retrieval", "answer", "citations"):
        values = [case[metric] for case in questions.values() if case[metric] is not None]
        metrics[metric] = {"passed": sum(values), "total": len(values)}
    ocr_checks = []
    for result in ocr.values():
        ocr_checks.extend(result["facts"].values())
        if "headers" in result:
            ocr_checks.extend([result["headers"], *result["rows"].values()])
    metrics["ocr"] = {"passed": sum(ocr_checks), "total": len(ocr_checks)}
    return {"top_k": top_k, "metrics": metrics, "questions": questions, "ocr": ocr}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("fixtures").add_argument("directory", type=Path)
    scoring = subcommands.add_parser("score")
    scoring.add_argument("observations", type=Path)
    scoring.add_argument("--top-k", type=int, default=5)
    arguments = parser.parse_args()
    if arguments.command == "fixtures":
        from fixtures import write_fixtures

        write_fixtures(arguments.directory)
        return 0
    if arguments.top_k < 1:
        parser.error("--top-k must be positive")
    try:
        report = score(
            json.loads(GOLD.read_text(encoding="utf-8")),
            json.loads(arguments.observations.read_text(encoding="utf-8")),
            top_k=arguments.top_k,
        )
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        parser.error("Invalid observations, see README.md for the schema")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return int(any(metric["passed"] != metric["total"] for metric in report["metrics"].values()))


if __name__ == "__main__":
    raise SystemExit(main())
