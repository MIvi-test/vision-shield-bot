"""Download a dataset by its manifest and verify every file's SHA-256.

    uv run python datasets/tools/fetch_dataset.py datasets/nature/manifest.jsonl

Files go to data/datasets/<dataset>/<id>.<ext>, outside Git. A file whose hash
differs from the manifest is deleted and reported; the exit code is then 1.
"""

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

from commons import (
    REPO_ROOT,
    HashMismatch,
    fetch_file,
    load_limits,
    open_url,
    sha256_of,
    with_retry,
)

EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


@dataclass
class Report:
    downloaded: list[str] = field(default_factory=list)
    present: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def read_manifest(path: Path) -> list[dict[str, Any]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def target_path(row: dict[str, Any], out_dir: Path) -> Path:
    suffix = Path(row["file_url"]).suffix.lower()
    if suffix not in EXTENSIONS:
        raise ValueError(f"unsupported file extension {suffix!r}")
    return out_dir / f"{row['id']}{suffix}"


def fetch_manifest(
    rows: list[dict[str, Any]],
    out_dir: Path,
    max_bytes: int,
    opener: Callable[[str], BinaryIO] = open_url,
    pause_s: float = 5.0,
) -> Report:
    out_dir.mkdir(parents=True, exist_ok=True)
    report = Report()
    for row in rows:
        row_id = row["id"]
        try:
            target = target_path(row, out_dir)
        except ValueError as error:
            report.errors.append(f"FAILED   {row_id}: {error}")
            continue
        if target.exists():
            if sha256_of(target) == row["sha256"]:
                report.present.append(row_id)
                continue
            target.unlink()
        try:
            with_retry(
                lambda: fetch_file(
                    row["file_url"],
                    target,
                    max_bytes,
                    expected_sha256=row["sha256"],
                    opener=opener,
                ),
                pause_s=pause_s,
            )
        except HashMismatch as error:
            report.errors.append(f"MISMATCH {row_id}: {error}")
        except (OSError, ValueError) as error:
            report.errors.append(f"FAILED   {row_id}: {error}")
        else:
            report.downloaded.append(row_id)
    return report


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("manifest", type=Path)
    parser.add_argument(
        "--out",
        type=Path,
        help="куда сохранить; по умолчанию data/datasets/<имя набора>",
    )
    args = parser.parse_args(argv)
    manifest: Path = args.manifest
    out_dir = args.out or REPO_ROOT / "data" / "datasets" / manifest.parent.name

    report = fetch_manifest(
        read_manifest(manifest), out_dir, load_limits().max_file_bytes
    )
    for line in report.errors:
        print(line)
    print(
        f"Скачано {len(report.downloaded)}, уже было {len(report.present)}, "
        f"ошибок {len(report.errors)}. Папка: {out_dir}"
    )
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
