"""S1-15: downloading a dataset by its manifest, without network access."""

import hashlib
import io
from pathlib import Path
from typing import Any

from fetch_dataset import fetch_manifest

BODY = b"\xff\xd8\xff" + b"forest" * 200
URL = "https://upload.wikimedia.org/wikipedia/commons/a/ab/Forest.jpg"


def row(**changes: Any) -> dict[str, Any]:
    return {
        "id": "nat-0001",
        "sha256": hashlib.sha256(BODY).hexdigest(),
        "file_url": URL,
        **changes,
    }


class FakeServer:
    """Serves fixed bytes and records requests; optionally fails first calls."""

    def __init__(self, body: bytes = BODY, failures: int = 0) -> None:
        self.body = body
        self.failures = failures
        self.requests: list[str] = []

    def __call__(self, url: str) -> io.BytesIO:
        self.requests.append(url)
        if len(self.requests) <= self.failures:
            raise OSError("getaddrinfo failed")
        return io.BytesIO(self.body)


def fetch(rows: list[dict[str, Any]], out: Path, server: FakeServer, **kwargs: Any):
    return fetch_manifest(
        rows, out, kwargs.get("max_bytes", 10_000), opener=server, pause_s=0
    )


def test_downloads_and_names_file_by_id(tmp_path: Path) -> None:
    report = fetch([row()], tmp_path, FakeServer())
    assert report.downloaded == ["nat-0001"]
    assert report.errors == []
    assert (tmp_path / "nat-0001.jpg").read_bytes() == BODY
    assert sorted(p.name for p in tmp_path.iterdir()) == ["nat-0001.jpg"]


def test_second_run_skips_verified_files(tmp_path: Path) -> None:
    fetch([row()], tmp_path, FakeServer())
    server = FakeServer()
    report = fetch([row()], tmp_path, server)
    assert report.present == ["nat-0001"]
    assert server.requests == []


def test_changed_local_file_is_downloaded_again(tmp_path: Path) -> None:
    (tmp_path / "nat-0001.jpg").write_bytes(b"edited locally")
    report = fetch([row()], tmp_path, FakeServer())
    assert report.downloaded == ["nat-0001"]
    assert (tmp_path / "nat-0001.jpg").read_bytes() == BODY


def test_hash_mismatch_is_reported_and_file_removed(tmp_path: Path) -> None:
    report = fetch([row()], tmp_path, FakeServer(body=b"\xff\xd8\xff replaced"))
    assert len(report.errors) == 1
    assert report.errors[0].startswith("MISMATCH nat-0001")
    assert list(tmp_path.iterdir()) == []


def test_network_errors_are_retried(tmp_path: Path) -> None:
    server = FakeServer(failures=2)
    report = fetch([row()], tmp_path, server)
    assert report.downloaded == ["nat-0001"]
    assert len(server.requests) == 3


def test_foreign_host_is_refused_without_request(tmp_path: Path) -> None:
    server = FakeServer()
    report = fetch([row(file_url="https://example.com/Forest.jpg")], tmp_path, server)
    assert report.errors and report.errors[0].startswith("FAILED   nat-0001")
    assert server.requests == []


def test_unsupported_extension_is_refused(tmp_path: Path) -> None:
    report = fetch([row(file_url=URL[:-4] + ".svg")], tmp_path, FakeServer())
    assert report.errors and "extension" in report.errors[0]


def test_oversized_file_is_refused(tmp_path: Path) -> None:
    report = fetch([row()], tmp_path, FakeServer(), max_bytes=100)
    assert report.errors and "exceeds" in report.errors[0]
    assert list(tmp_path.iterdir()) == []


def test_one_bad_row_does_not_stop_the_rest(tmp_path: Path) -> None:
    rows = [row(sha256="0" * 64), row(id="nat-0002")]
    report = fetch(rows, tmp_path, FakeServer())
    assert report.downloaded == ["nat-0002"]
    assert len(report.errors) == 1
