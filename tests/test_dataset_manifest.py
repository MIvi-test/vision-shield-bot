"""S1-15: dataset manifests follow the schema and the labeling rules."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from jsonschema.protocols import Validator

DATASETS = Path(__file__).resolve().parents[1] / "datasets"
MANIFESTS = sorted(DATASETS.glob("*/manifest.jsonl"))

VALID_ROW: dict[str, Any] = {
    "id": "nat-0001",
    "sha256": "a" * 64,
    "label": "suitable",
    "category": None,
    "split": "tuning",
    "source_url": "https://commons.wikimedia.org/wiki/File:Forest.jpg",
    "file_url": "https://upload.wikimedia.org/wikipedia/commons/a/ab/Forest.jpg",
    "license": "CC-BY-SA-4.0",
    "author": "Jane Doe",
    "model_release": None,
    "labeled_by": "dev4",
    "reviewed_by": None,
    "notes": "",
}


def schema_validator() -> Validator:
    schema = json.loads((DATASETS / "manifest.schema.json").read_text("utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def read_manifest(path: Path) -> list[dict[str, Any]]:
    """Parse JSONL; every non-final line must hold exactly one JSON object."""
    rows = []
    for number, line in enumerate(path.read_text("utf-8").splitlines(), start=1):
        assert line.strip(), f"{path.name}:{number}: пустая строка"
        row = json.loads(line)
        assert isinstance(row, dict), f"{path.name}:{number}: не объект JSON"
        rows.append(row)
    return rows


def row_problems(row: dict[str, Any]) -> list[str]:
    return [
        f"{list(e.absolute_path)}: {e.message}"
        for e in schema_validator().iter_errors(row)
    ]


def manifest_problems(rows: list[dict[str, Any]]) -> list[str]:
    """Rules that span several rows and cannot be expressed in the schema."""
    problems = []
    ids = [row["id"] for row in rows]
    if len(set(ids)) != len(ids):
        problems.append("id повторяется")
    hashes = [row["sha256"] for row in rows]
    if len(set(hashes)) != len(hashes):
        problems.append("sha256 повторяется: один файл записан дважды")
    if ids:
        prefix = ids[0].split("-")[0]
        expected = [f"{prefix}-{n:04d}" for n in range(1, len(ids) + 1)]
        if ids != expected:
            problems.append("id должны идти по порядку с 0001 без пропусков")
    splits_by_author: dict[str, set[str]] = {}
    for row in rows:
        splits_by_author.setdefault(row["author"], set()).add(row["split"])
    for author, splits in sorted(splits_by_author.items()):
        if len(splits) > 1:
            problems.append(f"автор {author!r} и в tuning, и в control")
    return problems


def row(**changes: Any) -> dict[str, Any]:
    result = copy.deepcopy(VALID_ROW)
    result.update(changes)
    return result


# --- real manifests --------------------------------------------------------


def test_manifests_exist() -> None:
    assert MANIFESTS, "нет ни одного datasets/*/manifest.jsonl"


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda p: p.parent.name)
def test_manifest_rows_match_schema(path: Path) -> None:
    for item in read_manifest(path):
        assert row_problems(item) == [], item.get("id")


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda p: p.parent.name)
def test_manifest_cross_row_rules(path: Path) -> None:
    assert manifest_problems(read_manifest(path)) == []


# --- the checks themselves -------------------------------------------------


@pytest.mark.parametrize(
    "item",
    [
        row(label="borderline", reviewed_by="dev3"),
        row(label="borderline", category="nsfw", reviewed_by="dev3"),
        row(label="prohibited", category="spam", reviewed_by="dev3"),
        row(license="CC0-1.0"),
        row(license="PDM-1.0"),
        row(model_release=True),
    ],
)
def test_schema_accepts_valid_rows(item: dict[str, Any]) -> None:
    assert row_problems(item) == []


@pytest.mark.parametrize(
    "item",
    [
        pytest.param(row(sha256="A" * 64), id="sha256-uppercase"),
        pytest.param(row(sha256="a" * 63), id="sha256-short"),
        pytest.param(row(label="nature"), id="unknown-label"),
        pytest.param(row(split="test"), id="unknown-split"),
        pytest.param(row(model_release=False), id="model-release-false"),
        pytest.param(row(label="borderline"), id="borderline-unreviewed"),
        pytest.param(
            row(label="prohibited", reviewed_by="dev3"), id="prohibited-no-category"
        ),
        pytest.param(
            row(label="prohibited", category="spam"), id="prohibited-unreviewed"
        ),
        pytest.param(row(category="nsfw"), id="suitable-with-category"),
        pytest.param(row(license="CC-BY-NC-4.0"), id="noncommercial-license"),
        pytest.param(row(author=""), id="empty-author"),
        pytest.param(row(author='<a href="x">Jane</a>'), id="html-author"),
        pytest.param(
            row(file_url="https://example.com/forest.jpg"), id="foreign-file-host"
        ),
        pytest.param(
            row(file_url=VALID_ROW["file_url"] + "?utm_source=x"), id="tracking-query"
        ),
        pytest.param(row(labeled_by="daniil"), id="unknown-labeler"),
        pytest.param(row(id="nat-1"), id="short-id"),
        pytest.param(row(extra="x"), id="extra-field"),
        pytest.param(
            {k: v for k, v in VALID_ROW.items() if k != "sha256"}, id="missing-sha256"
        ),
    ],
)
def test_schema_rejects_invalid_rows(item: dict[str, Any]) -> None:
    assert row_problems(item)


def test_cross_row_rules_accept_consistent_manifest() -> None:
    rows = [
        row(),
        row(id="nat-0002", sha256="b" * 64, author="John Roe", split="control"),
    ]
    assert manifest_problems(rows) == []


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        pytest.param(
            [row(), row(sha256="b" * 64)], "id повторяется", id="duplicate-id"
        ),
        pytest.param(
            [row(), row(id="nat-0002")], "sha256 повторяется", id="duplicate-sha256"
        ),
        pytest.param(
            [row(), row(id="nat-0003", sha256="b" * 64)],
            "по порядку",
            id="id-gap",
        ),
        pytest.param(
            [row(), row(id="nat-0002", sha256="b" * 64, split="control")],
            "и в tuning, и в control",
            id="author-in-both-splits",
        ),
    ],
)
def test_cross_row_rules_reject(rows: list[dict[str, Any]], expected: str) -> None:
    assert any(expected in problem for problem in manifest_problems(rows))


def test_read_manifest_rejects_blank_line(tmp_path: Path) -> None:
    path = tmp_path / "manifest.jsonl"
    path.write_text(json.dumps(VALID_ROW) + "\n\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="пустая строка"):
        read_manifest(path)
