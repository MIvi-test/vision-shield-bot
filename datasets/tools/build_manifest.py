"""Turn reviewed candidates into manifest rows.

Two steps, both work with data/candidates/<dataset>/ outside Git:

    uv run python datasets/tools/build_manifest.py template
        adds every new candidate to labels.txt with label "?"

    uv run python datasets/tools/build_manifest.py build
        appends labeled candidates to datasets/<dataset>/manifest.jsonl

labels.txt has one line per image: "<file> | <label> | <notes>". The label is
suitable, off_topic, borderline, borderline:<category> or skip. Lines starting
with # are comments. A person sets every label; this script never guesses one.
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from commons import REPO_ROOT

LABELS = {"suitable", "off_topic", "borderline"}
CATEGORIES = {"nsfw", "violence", "personal_data", "spam", "minors"}
SKIP = "skip"
UNSET = "?"
MAX_PER_AUTHOR = 5


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def parse_label(text: str) -> tuple[str, str | None]:
    """Return (label, category); raise ValueError for anything unknown."""
    label, _, category = text.strip().partition(":")
    if label not in LABELS | {SKIP, UNSET}:
        raise ValueError(f"неизвестная метка {text.strip()!r}")
    if not category:
        return label, None
    if label != "borderline" or category not in CATEGORIES:
        raise ValueError(
            f"{text.strip()!r}: категория бывает только у borderline, "
            f"одна из: {', '.join(sorted(CATEGORIES))}"
        )
    return label, category


def read_labels(path: Path) -> list[tuple[str, str, str | None, str]]:
    """Parse labels.txt into (file, label, category, notes)."""
    result = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 2:
            raise ValueError(f"labels.txt:{number}: нужно «файл | метка | заметка»")
        try:
            label, category = parse_label(parts[1])
        except ValueError as error:
            raise ValueError(f"labels.txt:{number}: {error}") from None
        notes = " | ".join(parts[2:]).strip()
        result.append((parts[0], label, category, notes))
    return result


def template_lines(candidates: list[dict[str, Any]], listed: set[str]) -> list[str]:
    lines = []
    for candidate in candidates:
        if candidate["local_file"] in listed:
            continue
        lines.append(f"# {candidate['found_in']}: {candidate['title']}")
        lines.append(f"{candidate['local_file']} | {UNSET} | ")
    return lines


def manifest_rows(
    candidates: list[dict[str, Any]],
    labels: list[tuple[str, str, str | None, str]],
    existing: list[dict[str, Any]],
    prefix: str,
) -> list[dict[str, Any]]:
    """Build new manifest rows; raise ValueError if labeling is incomplete."""
    by_file = {c["local_file"]: c for c in candidates}
    known = {row["source_url"] for row in existing}
    unset = [file for file, label, _, _ in labels if label == UNSET]
    if unset:
        raise ValueError(f"без метки: {len(unset)} файлов, например {unset[0]}")
    rows = []
    for file, label, category, notes in labels:
        if label == SKIP:
            continue
        candidate = by_file.get(file)
        if candidate is None:
            raise ValueError(f"{file}: нет в candidates.jsonl")
        if candidate["source_url"] in known:
            continue
        rows.append(
            {
                "id": f"{prefix}-{len(existing) + len(rows) + 1:04d}",
                "sha256": candidate["sha256"],
                "label": label,
                "category": category,
                "split": "tuning",
                "source_url": candidate["source_url"],
                "file_url": candidate["file_url"],
                "license": candidate["license"],
                "author": candidate["author"],
                "model_release": None,
                "labeled_by": "dev4",
                "reviewed_by": None,
                "notes": notes,
            }
        )
    return rows


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["template", "build"])
    parser.add_argument("--dataset", default="nature")
    parser.add_argument("--prefix", default="nat")
    args = parser.parse_args(argv)

    work = REPO_ROOT / "data" / "candidates" / args.dataset
    labels_file = work / "labels.txt"
    manifest = REPO_ROOT / "datasets" / args.dataset / "manifest.jsonl"
    candidates = read_jsonl(work / "candidates.jsonl")

    if args.command == "template":
        listed = (
            {file for file, *_ in read_labels(labels_file)}
            if labels_file.exists()
            else set()
        )
        lines = template_lines(candidates, listed)
        with labels_file.open("a", encoding="utf-8") as out:
            out.writelines(line + "\n" for line in lines)
        print(f"Добавлено в {labels_file}: {len(lines) // 2} файлов")
        return 0

    existing = read_jsonl(manifest)
    try:
        rows = manifest_rows(
            candidates, read_labels(labels_file), existing, args.prefix
        )
    except ValueError as error:
        print(f"Манифест не изменён: {error}")
        return 1
    with manifest.open("a", encoding="utf-8") as out:
        out.writelines(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    all_rows = existing + rows
    print(f"Добавлено строк: {len(rows)}, всего в манифесте: {len(all_rows)}")
    for (label, category), count in sorted(
        Counter((r["label"], r["category"]) for r in all_rows).items(),
        key=lambda item: (item[0][0], item[0][1] or ""),
    ):
        print(f"  {label}{':' + category if category else ''}: {count}")
    authors = Counter(r["author"] for r in all_rows)
    print(f"Авторов: {len(authors)}")
    for author, count in authors.most_common():
        if count > MAX_PER_AUTHOR:
            print(f"  ВНИМАНИЕ: {author!r} — {count} снимков, больше {MAX_PER_AUTHOR}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
