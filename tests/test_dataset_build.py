"""S1-15: building manifest rows from candidates and human labels."""

import json
from pathlib import Path
from typing import Any

import pytest
from build_manifest import manifest_rows, parse_label, read_labels, template_lines
from jsonschema import Draft202012Validator

SCHEMA = json.loads(
    (
        Path(__file__).resolve().parents[1] / "datasets" / "manifest.schema.json"
    ).read_text("utf-8")
)


def candidate(n: int, **changes: Any) -> dict[str, Any]:
    return {
        "title": f"File:Photo {n}.jpg",
        "source_url": f"https://commons.wikimedia.org/wiki/File:Photo_{n}.jpg",
        "file_url": f"https://upload.wikimedia.org/wikipedia/commons/a/ab/Photo_{n}.jpg",
        "sha256": f"{n:064x}",
        "license": "CC-BY-4.0",
        "author": f"Author {n}",
        "model_release": None,
        "width": 1000,
        "height": 800,
        "size": 1234,
        "local_file": f"{n:016x}.jpg",
        "found_in": "Quality images of forests",
        **changes,
    }


CANDIDATES = [candidate(1), candidate(2), candidate(3)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("suitable", ("suitable", None)),
        (" off_topic ", ("off_topic", None)),
        ("borderline", ("borderline", None)),
        ("borderline:nsfw", ("borderline", "nsfw")),
        ("skip", ("skip", None)),
        ("?", ("?", None)),
    ],
)
def test_parse_label_accepts(text: str, expected: tuple[str, str | None]) -> None:
    assert parse_label(text) == expected


@pytest.mark.parametrize(
    "text", ["prohibited", "nature", "suitable:nsfw", "borderline:weapons", ""]
)
def test_parse_label_rejects(text: str) -> None:
    with pytest.raises(ValueError):
        parse_label(text)


def test_read_labels_skips_comments_and_keeps_notes(tmp_path: Path) -> None:
    path = tmp_path / "labels.txt"
    path.write_text(
        "# Quality images of forests: File:Photo 1.jpg\n"
        "0000000000000001.jpg | suitable |\n"
        "\n"
        "0000000000000002.jpg | borderline:nsfw | пляж, люди далеко | лиц не видно\n",
        encoding="utf-8",
    )
    assert read_labels(path) == [
        ("0000000000000001.jpg", "suitable", None, ""),
        (
            "0000000000000002.jpg",
            "borderline",
            "nsfw",
            "пляж, люди далеко | лиц не видно",
        ),
    ]


def test_read_labels_reports_line_number(tmp_path: Path) -> None:
    path = tmp_path / "labels.txt"
    path.write_text("a.jpg | suitable\nb.jpg | forest\n", encoding="utf-8")
    with pytest.raises(ValueError, match="labels.txt:2"):
        read_labels(path)


def test_template_lists_only_new_candidates() -> None:
    lines = template_lines(CANDIDATES, {CANDIDATES[0]["local_file"]})
    assert lines == [
        "# Quality images of forests: File:Photo 2.jpg",
        "0000000000000002.jpg | ? | ",
        "# Quality images of forests: File:Photo 3.jpg",
        "0000000000000003.jpg | ? | ",
    ]


def labels(*items: tuple[int, str]) -> list[tuple[str, str, str | None, str]]:
    result = []
    for n, text in items:
        label, category = parse_label(text)
        result.append((f"{n:016x}.jpg", label, category, ""))
    return result


def test_rows_are_valid_and_numbered() -> None:
    rows = manifest_rows(
        CANDIDATES,
        labels((1, "suitable"), (2, "skip"), (3, "borderline:spam")),
        [],
        "nat",
    )
    assert [row["id"] for row in rows] == ["nat-0001", "nat-0002"]
    assert rows[1]["category"] == "spam"
    assert all(row["split"] == "tuning" and row["labeled_by"] == "dev4" for row in rows)
    validator = Draft202012Validator(SCHEMA)
    # reviewed_by is filled by Dev3 later, so borderline rows are not final yet.
    assert list(validator.iter_errors(rows[0])) == []
    assert list(validator.iter_errors({**rows[1], "reviewed_by": "dev3"})) == []


def test_numbering_continues_existing_manifest() -> None:
    existing = manifest_rows(CANDIDATES[:1], labels((1, "suitable")), [], "nat")
    rows = manifest_rows(
        CANDIDATES, labels((1, "suitable"), (2, "off_topic")), existing, "nat"
    )
    assert [row["id"] for row in rows] == ["nat-0002"]


def test_unlabeled_file_stops_the_build() -> None:
    with pytest.raises(ValueError, match="без метки"):
        manifest_rows(CANDIDATES, labels((1, "suitable"), (2, "?")), [], "nat")


def test_unknown_file_stops_the_build() -> None:
    with pytest.raises(ValueError, match="нет в candidates.jsonl"):
        manifest_rows(CANDIDATES, labels((9, "suitable")), [], "nat")
