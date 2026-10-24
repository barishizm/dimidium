from typing import Any

import pytest
from pydantic import ValidationError

from app.functions import (
    MAX_OWNER_OPTIONS,
    TOOL_DEFINITIONS,
    TOOL_INPUT_MODELS,
    AskOwnerInput,
    EndCallInput,
    OwnerOption,
    SaveMessageInput,
    resolve_owner_reply,
)

OPTIONS = [
    {"id": "door", "label_tr": "Kapıya bırak", "meaning_en": "Leave the parcel at the door."},
    {"id": "locker", "label_tr": "Paketomata", "meaning_en": "Deliver to a parcel locker."},
]


def ask_owner_input(**overrides: Any) -> dict[str, Any]:
    data = {
        "summary_tr": "DPD kurye, paket teslimatı. Evde misiniz diye soruyor.",
        "caller_said_lt": "ar esate namuose?",
        "options": OPTIONS,
    }
    return data | overrides


def test_every_definition_has_a_model_and_matching_fields() -> None:
    assert [d["name"] for d in TOOL_DEFINITIONS] == list(TOOL_INPUT_MODELS)
    for definition in TOOL_DEFINITIONS:
        schema = definition["input_schema"]
        model = TOOL_INPUT_MODELS[definition["name"]]
        assert set(schema["properties"]) == set(model.model_fields)
        required = {name for name, f in model.model_fields.items() if f.is_required()}
        assert set(schema["required"]) == required
        assert schema["additionalProperties"] is False


def test_option_schema_matches_model() -> None:
    items = TOOL_DEFINITIONS[0]["input_schema"]["properties"]["options"]["items"]
    assert set(items["properties"]) == set(OwnerOption.model_fields)
    assert set(items["required"]) == set(OwnerOption.model_fields)


def test_ask_owner_valid() -> None:
    parsed = AskOwnerInput.model_validate(ask_owner_input())
    assert [o.id for o in parsed.options] == ["door", "locker"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"options": []},
        {"options": [OPTIONS[0]] * 2},
        {"options": [{**OPTIONS[0], "id": f"o{i}"} for i in range(MAX_OWNER_OPTIONS + 1)]},
        {"options": [{"id": "x", "label_tr": "X"}]},
        {"summary_tr": "   "},
        {"unexpected": "field"},
    ],
)
def test_ask_owner_invalid(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        AskOwnerInput.model_validate(ask_owner_input(**overrides))


def test_ask_owner_missing_field() -> None:
    data = ask_owner_input()
    del data["caller_said_lt"]
    with pytest.raises(ValidationError):
        AskOwnerInput.model_validate(data)


def test_save_message_only_purpose_required() -> None:
    msg = SaveMessageInput.model_validate({"purpose": "Parcel delivery"})
    assert msg.caller_name is None
    with pytest.raises(ValidationError):
        SaveMessageInput.model_validate({"caller_name": "Jonas"})


def test_end_call_requires_reason() -> None:
    with pytest.raises(ValidationError):
        EndCallInput.model_validate({})


@pytest.mark.parametrize("reply", ["door", "DOOR", " 1 "])
def test_resolve_owner_reply_by_id_or_number(reply: str) -> None:
    options = [OwnerOption.model_validate(o) for o in OPTIONS]
    result = resolve_owner_reply(options, reply)
    assert result.decision_text_en == "Leave the parcel at the door."
    assert result.free_text_original is None


def test_resolve_owner_reply_free_text() -> None:
    options = [OwnerOption.model_validate(o) for o in OPTIONS]
    result = resolve_owner_reply(options, "Komşuya bıraksın, 12 numara")
    assert result.free_text_original == "Komşuya bıraksın, 12 numara"
    assert "Komşuya bıraksın, 12 numara" in result.decision_text_en


def test_resolve_owner_reply_out_of_range_number_is_free_text() -> None:
    options = [OwnerOption.model_validate(o) for o in OPTIONS]
    assert resolve_owner_reply(options, "3").free_text_original == "3"
