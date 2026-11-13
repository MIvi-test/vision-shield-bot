"""S1-10: validation of moderation policy files against schemas and shared rules."""

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from jsonschema.protocols import Validator

POLICIES = Path(__file__).resolve().parents[1] / "packages" / "contracts" / "policies"
CAMPAIGN_FILES = sorted((POLICIES / "campaigns").glob("*.json"))

MANDATORY_PROHIBITED = {"violence", "personal_data", "spam", "minors"}
TECHNICAL_STAGES = {"fetcher", "media"}
# Words that would reveal how the check works; the author sees neutral text only.
FORBIDDEN_USER_WORDS = re.compile(
    r"score|порог|модел|нейросет|nsfw|clip|sha|хеш|хэш|threshold", re.IGNORECASE
)
ALLOWED_PLACEHOLDERS = {"theme_title"}


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def validator(schema_name: str) -> Validator:
    schema = load(POLICIES / schema_name)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def errors(v: Validator, data: Any) -> list[str]:
    return [f"{list(e.absolute_path)}: {e.message}" for e in v.iter_errors(data)]


# --- media profile ---------------------------------------------------------


def test_media_profile_matches_schema() -> None:
    profile = load(POLICIES / "media-profile-v0.json")
    assert errors(validator("media-profile.schema.json"), profile) == []


def test_media_profile_values_are_consistent() -> None:
    p = load(POLICIES / "media-profile-v0.json")
    inp, dec, out = p["input"], p["decode"], p["output"]
    assert inp["min_side_px"] <= out["max_side_px"] <= inp["max_side_px"]
    assert inp["max_pixels"] <= inp["max_side_px"] ** 2
    assert dec["timeout_ms"] < dec["task_timeout_ms"]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("input", "formats"), ["gif"]),
        (("input", "max_frames"), 2),
        (("input", "max_file_bytes"), 100 * 1024 * 1024),
        (("output", "strip_metadata"), False),
        (("output", "max_side_px"), 8000),
        (("input", "unknown_field"), 1),
    ],
)
def test_media_profile_rejects_unsafe_values(path: tuple[str, str], value: Any) -> None:
    profile = copy.deepcopy(load(POLICIES / "media-profile-v0.json"))
    profile[path[0]][path[1]] = value
    assert errors(validator("media-profile.schema.json"), profile)


# --- reason codes ----------------------------------------------------------


def reason_codes() -> dict[str, dict[str, Any]]:
    return load(POLICIES / "reason-codes.json")["codes"]


def test_reason_codes_match_schema() -> None:
    data = load(POLICIES / "reason-codes.json")
    assert errors(validator("reason-codes.schema.json"), data) == []


def test_technical_rejections_cannot_be_approved_by_moderator() -> None:
    for code, reason in reason_codes().items():
        if reason["stage"] in TECHNICAL_STAGES or code == "DUPLICATE":
            assert reason["effect"] == "rejected", code
            assert reason["moderator_can_approve"] is False, code
        if reason["moderator_can_approve"] is False:
            assert reason["effect"] == "rejected", code


def test_review_codes_share_one_neutral_message() -> None:
    messages = {
        r["user_message_ru"]
        for r in reason_codes().values()
        if r["effect"] == "review_required"
    }
    assert len(messages) == 1


def test_user_messages_do_not_reveal_checks() -> None:
    for code, reason in reason_codes().items():
        text = reason["user_message_ru"]
        assert not FORBIDDEN_USER_WORDS.search(text), code
        assert set(re.findall(r"{(\w+)}", text)) <= ALLOWED_PLACEHOLDERS, code


def test_file_size_message_matches_profile() -> None:
    limit_mb = load(POLICIES / "media-profile-v0.json")["input"]["max_file_bytes"]
    text = reason_codes()["FILE_TOO_LARGE"]["user_message_ru"]
    assert f"до {limit_mb // (1024 * 1024)} МБ" in text


# --- campaign policies -----------------------------------------------------


def test_campaign_files_exist() -> None:
    assert CAMPAIGN_FILES


@pytest.mark.parametrize("path", CAMPAIGN_FILES, ids=lambda p: p.name)
def test_campaign_matches_schema(path: Path) -> None:
    assert errors(validator("campaign-policy.schema.json"), load(path)) == []


@pytest.mark.parametrize("path", CAMPAIGN_FILES, ids=lambda p: p.name)
def test_campaign_file_name_and_rules(path: Path) -> None:
    policy = load(path)
    assert path.stem == policy["policy_version"]
    assert policy["policy_version"].startswith(policy["campaign_slug"] + "-v")

    categories = [item["category"] for item in policy["cv"]["prohibited"]]
    assert len(categories) == len(set(categories))
    assert MANDATORY_PROHIBITED <= set(categories)

    t = policy["cv"]["thresholds"]
    if t["calibrated"]:
        assert t["topic_reject_max"] < t["topic_approve_min"]
        assert t["nsfw_review_min"] <= t["nsfw_reject_min"]


def test_auto_mode_requires_calibration() -> None:
    v = validator("campaign-policy.schema.json")
    policy = copy.deepcopy(load(POLICIES / "campaigns" / "nature-v0.json"))
    policy["mode"] = "auto"
    assert errors(v, policy), "auto без калибровки должен отклоняться"

    policy["status"] = "approved"
    policy["cv"]["thresholds"] = {
        "calibrated": True,
        "calibration_report": "docs/results/cv-thresholds-nature-v1.md",
        "topic_approve_min": 0.8,
        "topic_reject_max": 0.3,
        "nsfw_reject_min": 0.9,
        "nsfw_review_min": 0.5,
        "prohibited_review_min": 0.6,
    }
    assert errors(v, policy) == []

    policy["cv"]["thresholds"]["topic_approve_min"] = None
    assert errors(v, policy), "откалиброванные пороги не могут быть пустыми"


def test_prohibited_action_is_review_only() -> None:
    policy = copy.deepcopy(load(POLICIES / "campaigns" / "nature-v0.json"))
    policy["cv"]["prohibited"][0]["action"] = "reject"
    assert errors(validator("campaign-policy.schema.json"), policy)
