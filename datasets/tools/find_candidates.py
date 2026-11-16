"""Find and download candidate images from a Wikimedia Commons category.

Candidates are stored outside Git in data/candidates/<dataset>/ together with
candidates.jsonl. A person reviews each image and assigns the label; only then
does a row go into datasets/<dataset>/manifest.jsonl.

    uv run python datasets/tools/find_candidates.py --category "Quality images of forests" --limit 5
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from commons import (
    REPO_ROOT,
    Candidate,
    category_files,
    download_with_retry,
    evaluate,
    load_limits,
)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def known_sources(dataset: str, candidates_file: Path) -> set[str]:
    """Commons pages already in the manifest or among earlier candidates."""
    manifest = REPO_ROOT / "datasets" / dataset / "manifest.jsonl"
    rows = read_jsonl(manifest) + read_jsonl(candidates_file)
    return {row["source_url"] for row in rows}


def candidate_record(
    candidate: Candidate, sha256: str, local_file: Path, category: str
) -> dict[str, Any]:
    return {
        "title": candidate.title,
        "source_url": candidate.source_url,
        "file_url": candidate.file_url,
        "sha256": sha256,
        "license": candidate.license,
        "author": candidate.author,
        "model_release": None,
        "width": candidate.width,
        "height": candidate.height,
        "size": candidate.size,
        "local_file": local_file.name,
        "found_in": category,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--category", required=True, help="без префикса Category:")
    parser.add_argument("--limit", type=int, default=10, help="сколько скачать")
    parser.add_argument("--dataset", default="nature")
    parser.add_argument(
        "--max-per-author",
        type=int,
        default=2,
        help="не больше N файлов одного автора за запуск",
    )
    parser.add_argument(
        "--scan-limit",
        type=int,
        default=300,
        help="сколько файлов категории просмотреть максимум",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    limits = load_limits()
    out_dir = REPO_ROOT / "data" / "candidates" / args.dataset
    out_dir.mkdir(parents=True, exist_ok=True)
    candidates_file = out_dir / "candidates.jsonl"
    seen = known_sources(args.dataset, candidates_file)

    skipped: Counter[str] = Counter()
    per_author: Counter[str] = Counter()
    saved = scanned = 0
    for page in category_files(args.category):
        if saved >= args.limit or scanned >= args.scan_limit:
            break
        scanned += 1
        candidate, reason = evaluate(page, limits)
        if candidate is None:
            skipped[reason] += 1
            continue
        if candidate.source_url in seen:
            skipped["already known"] += 1
            continue
        if per_author[candidate.author] >= args.max_per_author:
            skipped["author limit"] += 1
            continue
        local_file = out_dir / f"{candidate.sha1[:16]}.{candidate.extension}"
        try:
            sha256 = download_with_retry(candidate, local_file, limits.max_file_bytes)
        except (OSError, ValueError) as error:
            skipped[f"download failed: {error}"] += 1
            continue
        record = candidate_record(candidate, sha256, local_file, args.category)
        with candidates_file.open("a", encoding="utf-8") as out:
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
        seen.add(candidate.source_url)
        per_author[candidate.author] += 1
        saved += 1
        print(f"saved   {local_file.name}  {candidate.license:<13} {candidate.title}")

    print(
        f"\nПросмотрено {scanned}, скачано {saved}, пропущено {sum(skipped.values())}."
    )
    for reason, count in skipped.most_common():
        print(f"  {count:>3}  {reason}")
    print(f"Кандидаты: {candidates_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
