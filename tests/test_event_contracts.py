"""S1-08: event contracts between Backend and media/CV workers."""

import copy
import json
import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

CONTRACTS = Path(__file__).resolve().parents[1] / "packages" / "contracts"
EVENTS = CONTRACTS / "events"
POLICIES = CONTRACTS / "policies"
EXAMPLES = sorted((EVENTS / "examples").glob("*.json"))
BASE = "https://contracts.vision-shield-bot.example/events/"
RESULT_TYPES = {"completed", "rejected", "failed"}


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def all_schema_files() -> list[Path]:
    return sorted(EVENTS.glob("*.schema.json")) + sorted(POLICIES.glob("*.schema.json"))


REGISTRY: Registry = Registry().with_resources(
    (load(p)["$id"], Resource.from_contents(load(p))) for p in all_schema_files()
)
EVENT_TYPES: list[str] = load(EVENTS / "envelope.schema.json")["properties"][
    "event_type"
]["enum"]


def event_errors(event: Any) -> list[str]:
    schema = REGISTRY[BASE + event["event_type"] + ".schema.json"].contents
    validator = Draft202012Validator(schema, registry=REGISTRY)
    return [
        f"{list(e.absolute_path)}: {e.message}" for e in validator.iter_errors(event)
    ]


def example(name: str) -> dict[str, Any]:
    return copy.deepcopy(load(EVENTS / "examples" / f"{name}.json"))


# --- schemas ---------------------------------------------------------------


@pytest.mark.parametrize("path", all_schema_files(), ids=lambda p: p.name)
def test_schema_is_valid_and_id_matches_file(path: Path) -> None:
    schema = load(path)
    Draft202012Validator.check_schema(schema)
    assert schema["$id"].endswith("/" + path.parent.name + "/" + path.name)


def test_every_event_type_has_schema_and_example() -> None:
    covered = {load(p)["event_type"] for p in EXAMPLES}
    for event_type in EVENT_TYPES:
        assert (EVENTS / f"{event_type}.schema.json").exists(), event_type
        assert event_type in covered, event_type


# --- examples --------------------------------------------------------------


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_matches_schema(path: Path) -> None:
    event = load(path)
    assert path.name.startswith(event["event_type"] + ".")
    assert event_errors(event) == []


def test_examples_form_consistent_chains() -> None:
    events = [load(p) for p in EXAMPLES]
    requests = {
        e["event_id"]: e for e in events if e["event_type"].endswith(".requested")
    }
    for e in events:
        assert e["correlation_id"] == e["submission_id"]
        stage, kind = e["event_type"].split(".")
        if kind not in RESULT_TYPES:
            continue
        task = requests[e["causation_id"]]
        assert task["event_type"] == f"{stage}.requested"
        assert task["operation_id"] == e["operation_id"]
        assert task["payload"]["asset_id"] == e["payload"]["asset_id"]
        expected_id = uuid.uuid5(
            uuid.UUID(e["operation_id"]), f"{e['producer']}.result"
        )
        assert e["event_id"] == str(expected_id), "ID результата должен быть uuid5"


def test_one_result_per_operation() -> None:
    results = [
        load(p) for p in EXAMPLES if load(p)["event_type"].split(".")[1] in RESULT_TYPES
    ]
    operations = [r["operation_id"] for r in results]
    assert len(operations) == len(set(operations))


# --- consistency with policies and README ----------------------------------


def test_reason_code_enums_match_reason_table() -> None:
    codes = load(POLICIES / "reason-codes.json")["codes"]
    defs = load(EVENTS / "common.schema.json")["$defs"]
    media = {c for c, r in codes.items() if r["stage"] == "media"} | {"FILE_TOO_LARGE"}
    cv = {c for c, r in codes.items() if r["stage"] == "cv"}
    assert set(defs["media_reason_code"]["enum"]) == media
    assert set(defs["cv_reason_code"]["enum"]) == cv


def test_worker_error_codes_documented_in_readme() -> None:
    readme = (EVENTS / "README.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"^\| `([A-Z_]+)` \|", readme, flags=re.MULTILINE))
    schema_codes = set(
        load(EVENTS / "common.schema.json")["$defs"]["worker_error"]["properties"][
            "code"
        ]["enum"]
    )
    assert schema_codes <= documented


# --- negative cases --------------------------------------------------------


def set_path(data: dict[str, Any], path: str, value: Any) -> None:
    *parents, last = path.split(".")
    for key in parents:
        data = data[key]
    data[last] = value


@pytest.mark.parametrize(
    ("name", "path", "value"),
    [
        ("media.requested.webp", "payload.source.key", "https://evil.example/x.jpg"),
        ("media.requested.webp", "payload.source.key", "submissions/../../etc/passwd"),
        ("media.requested.webp", "payload.source.key", "/submissions/x"),
        ("media.requested.webp", "payload.target.bucket", "quarantine"),
        ("media.requested.webp", "payload.source.bucket", "sanitized"),
        ("media.requested.webp", "payload.media_profile.input.formats", ["gif"]),
        ("media.requested.webp", "payload.url", "https://example.com"),
        ("media.requested.webp", "token", "secret"),
        ("media.requested.webp", "occurred_at", "2026-11-13T10:00:00+03:00"),
        ("media.requested.webp", "schema_version", 2),
        ("media.requested.webp", "producer", "media-worker"),
        ("media.completed.webp", "payload.metadata_removed", False),
        ("media.completed.webp", "payload.sanitized.format", "png"),
        ("media.completed.webp", "payload.sanitized.bucket", "quarantine"),
        ("media.completed.webp", "producer", "cv-worker"),
        ("media.rejected.unsupported-format", "payload.reason_codes", ["OFF_TOPIC"]),
        ("media.rejected.unsupported-format", "payload.reason_codes", []),
        ("media.failed.storage-unavailable", "payload.error.detail", "see https://x.y"),
        ("media.failed.storage-unavailable", "payload.error.code", "UNKNOWN"),
        ("cv.requested.nature", "payload.image.bucket", "quarantine"),
        ("cv.requested.nature", "payload.policy.cv.thresholds.calibrated", False),
        ("cv.completed.review", "payload.scores.topic", 1.5),
        ("cv.completed.review", "payload.reason_codes", ["UNSUPPORTED_FORMAT"]),
        ("cv.completed.review", "payload.models.topic.source", "https://x.y/model"),
    ],
)
def test_broken_event_is_rejected(name: str, path: str, value: Any) -> None:
    event = example(name)
    set_path(event, path, value)
    assert event_errors(event), f"{name}: {path}={value!r} должен отклоняться"


def test_missing_operation_id_is_rejected() -> None:
    event = example("media.requested.webp")
    del event["operation_id"]
    assert event_errors(event)


def test_approval_only_with_topic_match() -> None:
    event = example("cv.completed.review")
    event["payload"]["recommended_decision"] = "approved"
    event["payload"]["reason_codes"] = ["TOPIC_MATCH", "NSFW_SUSPECTED"]
    assert event_errors(event)
    event["payload"]["reason_codes"] = ["TOPIC_MATCH"]
    assert event_errors(event) == []
