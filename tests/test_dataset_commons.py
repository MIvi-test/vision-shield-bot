"""S1-15: Commons selection rules and downloads, without network access."""

import copy
import dataclasses
import hashlib
import io
import json
from pathlib import Path
from typing import Any

import pytest
from commons import (
    Candidate,
    Limits,
    category_files,
    download,
    download_with_retry,
    evaluate,
    load_limits,
    plain_text,
    spdx_license,
    without_query,
)

LIMITS = Limits(
    max_file_bytes=10_485_760,
    max_pixels=25_000_000,
    max_side_px=8000,
    min_side_px=512,
    mime_types=frozenset({"image/jpeg", "image/png", "image/webp"}),
)

# Shape recorded from the Commons API (prop=imageinfo, formatversion=2).
PAGE: dict[str, Any] = {
    "pageid": 1,
    "ns": 6,
    "title": "File:Forest path.jpg",
    "imageinfo": [
        {
            "size": 6_030_563,
            "width": 3647,
            "height": 2733,
            "mime": "image/jpeg",
            "sha1": "e3961ddcc866" + "0" * 28,
            "url": "https://upload.wikimedia.org/wikipedia/commons/2/21/Forest_path.jpg"
            "?utm_source=commons.wikimedia.org&utm_campaign=imageinfo",
            "descriptionurl": "https://commons.wikimedia.org/wiki/File:Forest_path.jpg",
            "extmetadata": {
                "LicenseShortName": {"value": "CC BY-SA 4.0"},
                "Artist": {
                    "value": '<bdi><a href="//commons.wikimedia.org/wiki/User:X">'
                    "Thomas Bresson</a></bdi>"
                },
                "Restrictions": {"value": ""},
            },
        }
    ],
}


def page(**info_changes: Any) -> dict[str, Any]:
    result = copy.deepcopy(PAGE)
    info = result["imageinfo"][0]
    for key, value in info_changes.items():
        if key in {"LicenseShortName", "Artist", "Restrictions"}:
            info["extmetadata"][key] = {"value": value}
        else:
            info[key] = value
    return result


def fake_opener(responses: dict[str, bytes]):
    def opener(url: str) -> io.BytesIO:
        for prefix, body in responses.items():
            if url.startswith(prefix):
                return io.BytesIO(body)
        raise AssertionError(f"unexpected request: {url}")

    return opener


# --- pure rules ------------------------------------------------------------


@pytest.mark.parametrize(
    ("short_name", "expected"),
    [
        ("CC BY 4.0", "CC-BY-4.0"),
        ("CC BY-SA 3.0", "CC-BY-SA-3.0"),
        ("CC BY 2.5", "CC-BY-2.5"),
        ("CC0", "CC0-1.0"),
        ("Public domain", "PDM-1.0"),
        ("CC BY-NC 4.0", None),
        ("CC BY-SA 3.0 de", None),
        ("GFDL", None),
        ("", None),
    ],
)
def test_spdx_license(short_name: str, expected: str | None) -> None:
    assert spdx_license(short_name) == expected


def test_plain_text_strips_html_and_entities() -> None:
    raw = '<bdi><a href="//x">Jos&eacute;   Smith</a></bdi>\n'
    assert plain_text(raw) == "José Smith"


def test_without_query_keeps_path_only() -> None:
    url = "https://upload.wikimedia.org/wikipedia/commons/a/ab/X.jpg?utm_source=y#z"
    expected = "https://upload.wikimedia.org/wikipedia/commons/a/ab/X.jpg"
    assert without_query(url, "upload.wikimedia.org") == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://upload.wikimedia.org/x.jpg",
        "https://upload.wikimedia.org.evil.example/x.jpg",
        "https://example.com/x.jpg",
    ],
)
def test_without_query_rejects_other_hosts(url: str) -> None:
    with pytest.raises(ValueError):
        without_query(url, "upload.wikimedia.org")


def test_load_limits_reads_media_profile() -> None:
    limits = load_limits()
    assert limits.max_file_bytes == 10_485_760
    assert "image/jpeg" in limits.mime_types


def test_evaluate_accepts_suitable_file() -> None:
    candidate, reason = evaluate(PAGE, LIMITS)
    assert reason == "ok"
    assert candidate is not None
    assert candidate.file_url == (
        "https://upload.wikimedia.org/wikipedia/commons/2/21/Forest_path.jpg"
    )
    assert candidate.source_url.startswith("https://commons.wikimedia.org/wiki/File:")
    assert candidate.license == "CC-BY-SA-4.0"
    assert candidate.author == "Thomas Bresson"
    assert candidate.extension == "jpg"


@pytest.mark.parametrize(
    ("item", "reason"),
    [
        pytest.param(
            page(Restrictions="personality"), "identifiable people", id="people"
        ),
        pytest.param(page(size=48_789_062), "file too large", id="big-file"),
        pytest.param(page(width=8148, height=3412), "too many pixels", id="big-image"),
        pytest.param(page(width=9000, height=500), "too many pixels", id="long-side"),
        pytest.param(page(width=800, height=400), "too small", id="small-side"),
        pytest.param(page(mime="image/gif"), "format image/gif", id="gif"),
        pytest.param(page(mime="image/tiff"), "format image/tiff", id="tiff"),
        pytest.param(page(LicenseShortName="CC BY-NC 4.0"), "license", id="nc"),
        pytest.param(page(Artist=""), "no author", id="no-author"),
        pytest.param(
            page(url="https://example.com/x.jpg"), "bad url", id="foreign-file-host"
        ),
        pytest.param(
            {"title": "File:X.jpg", "missing": True}, "no file info", id="gone"
        ),
    ],
)
def test_evaluate_skips_with_reason(item: dict[str, Any], reason: str) -> None:
    candidate, actual = evaluate(item, LIMITS)
    assert candidate is None
    assert actual.startswith(reason)


def test_category_files_follows_continuation() -> None:
    first = {"continue": {"gcmcontinue": "next", "continue": "gcmcontinue||"}}
    first["query"] = {"pages": [PAGE]}
    second = {"query": {"pages": [page(sha1="f" * 40)]}}
    requests: list[str] = []

    def opener(url: str) -> io.BytesIO:
        requests.append(url)
        body = second if "gcmcontinue=next" in url else first
        return io.BytesIO(json.dumps(body).encode())

    pages = list(category_files("Forests", opener=opener, pause_s=0))
    assert len(pages) == 2
    assert len(requests) == 2
    assert "Category%3AForests" in requests[0]


# --- downloads -------------------------------------------------------------


def candidate_for(body: bytes, **changes: Any) -> Candidate:
    candidate, _ = evaluate(PAGE, LIMITS)
    assert candidate is not None
    with_hash = dataclasses.replace(candidate, sha1=hashlib.sha1(body).hexdigest())
    return dataclasses.replace(with_hash, **changes)


def test_download_saves_file_and_returns_sha256(tmp_path: Path) -> None:
    body = b"\xff\xd8\xff" + b"x" * 1000
    target = tmp_path / "a.jpg"
    opener = fake_opener({"https://upload.wikimedia.org/": body})
    sha256 = download(candidate_for(body), target, 10_000, opener=opener)
    assert sha256 == hashlib.sha256(body).hexdigest()
    assert target.read_bytes() == body
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize(
    ("max_bytes", "changes", "message"),
    [
        pytest.param(100, {}, "exceeds", id="too-large"),
        pytest.param(10_000, {"sha1": "0" * 40}, "SHA-1", id="sha1-mismatch"),
        pytest.param(
            10_000, {"file_url": "https://example.com/a.jpg"}, "refusing", id="host"
        ),
    ],
)
def test_download_rejects_and_leaves_nothing(
    tmp_path: Path, max_bytes: int, changes: dict[str, Any], message: str
) -> None:
    body = b"\xff\xd8\xff" + b"x" * 1000
    opener = fake_opener({"https://upload.wikimedia.org/": body})
    with pytest.raises(ValueError, match=message):
        download(
            candidate_for(body, **changes),
            tmp_path / "a.jpg",
            max_bytes,
            opener=opener,
        )
    assert list(tmp_path.iterdir()) == []


def test_download_with_retry_repeats_network_errors(tmp_path: Path) -> None:
    body = b"\xff\xd8\xff" + b"x" * 1000
    calls: list[str] = []

    def flaky(url: str) -> io.BytesIO:
        calls.append(url)
        if len(calls) < 3:
            raise OSError("getaddrinfo failed")
        return io.BytesIO(body)

    target = tmp_path / "a.jpg"
    sha256 = download_with_retry(
        candidate_for(body), target, 10_000, opener=flaky, pause_s=0
    )
    assert sha256 == hashlib.sha256(body).hexdigest()
    assert len(calls) == 3


def test_download_with_retry_gives_up_after_attempts(tmp_path: Path) -> None:
    def broken(url: str) -> io.BytesIO:
        raise OSError("connection reset")

    with pytest.raises(OSError):
        download_with_retry(
            candidate_for(b"x"), tmp_path / "a.jpg", 10_000, opener=broken, pause_s=0
        )
    assert list(tmp_path.iterdir()) == []


def test_download_with_retry_does_not_repeat_bad_content(tmp_path: Path) -> None:
    body = b"\xff\xd8\xff" + b"x" * 1000
    calls: list[str] = []

    def opener(url: str) -> io.BytesIO:
        calls.append(url)
        return io.BytesIO(body)

    with pytest.raises(ValueError, match="SHA-1"):
        download_with_retry(
            candidate_for(body, sha1="0" * 40),
            tmp_path / "a.jpg",
            10_000,
            opener=opener,
            pause_s=0,
        )
    assert len(calls) == 1
