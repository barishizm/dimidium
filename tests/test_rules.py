from pathlib import Path

import pytest
from pydantic import ValidationError

from app.rules import ContactAction, RulesConfig, RulesEngine, normalize_number

EXAMPLE = Path(__file__).resolve().parents[1] / "rules.example.yaml"

RULES_YAML = """
known_numbers:
  - number: "+370 600 00001"
    name: Aistė
    relation: partner
  - number: "8 600 00009"
    name: Pestering Ex
    action: polite_reject
keyword_rules:
  - name: courier
    keywords: [kurjer, DPD]
    allowed_actions: [leave at the door]
    guidance: Ask which company.
  - name: sales
    keywords: [pasiūlym]
    action: polite_reject
"""


@pytest.fixture
def engine(tmp_path: Path) -> RulesEngine:
    path = tmp_path / "rules.yaml"
    path.write_text(RULES_YAML, encoding="utf-8")
    return RulesEngine.from_yaml(path)


@pytest.mark.parametrize(
    "raw",
    ["+37060000001", "+370 600 00001", "0037060000001", "860000001", "(+370) 600-00001"],
)
def test_normalize_number_formats(raw: str) -> None:
    assert normalize_number(raw) == "37060000001"


def test_known_number_matches_across_formats(engine: RulesEngine) -> None:
    contact = engine.match_number("860000001")
    assert contact is not None
    assert contact.name == "Aistė"
    assert contact.action is ContactAction.NOTIFY_IMMEDIATELY


def test_national_format_in_yaml_matches_e164_caller(engine: RulesEngine) -> None:
    contact = engine.match_number("+37060000009")
    assert contact is not None and contact.action is ContactAction.POLITE_REJECT


def test_unknown_and_withheld_numbers(engine: RulesEngine) -> None:
    assert engine.match_number("+37069999999") is None
    assert engine.match_number(None) is None
    assert engine.match_number("") is None


def test_keyword_match_is_case_insensitive(engine: RulesEngine) -> None:
    names = [r.name for r in engine.match_keywords("Sveiki, esu KURJERIS iš dpd")]
    assert names == ["courier"]
    assert [r.name for r in engine.match_keywords("Turime jums pasiūlymą")] == ["sales"]
    assert engine.match_keywords("Labas") == []


def test_summary_for_trusted_contact_skips_screening(engine: RulesEngine) -> None:
    summary = engine.summary_for_call("+37060000001")
    assert "Aistė" in summary
    assert "Do not screen" in summary


def test_summary_for_unknown_caller(engine: RulesEngine) -> None:
    summary = engine.summary_for_call("+37069999999")
    assert "not in contacts" in summary
    assert "courier" in summary and "leave at the door" in summary
    assert "sales" in summary and "polite_reject" in summary


def test_summary_never_contains_phone_numbers(engine: RulesEngine) -> None:
    for caller in ("+37060000001", "+37060000009", "+37069999999", None):
        summary = engine.summary_for_call(caller)
        assert "60000001" not in summary
        assert "69999999" not in summary
        assert "60000009" not in summary


def test_summary_for_withheld_number(engine: RulesEngine) -> None:
    assert "withheld" in engine.summary_for_call(None)


def test_missing_file_gives_empty_rules(tmp_path: Path) -> None:
    engine = RulesEngine.from_yaml(tmp_path / "nope.yaml")
    assert engine.config == RulesConfig()
    assert "not in contacts" in engine.summary_for_call("+37060000001")


def test_empty_file_gives_empty_rules(tmp_path: Path) -> None:
    path = tmp_path / "rules.yaml"
    path.write_text("", encoding="utf-8")
    assert RulesEngine.from_yaml(path).config == RulesConfig()


@pytest.mark.parametrize(
    "bad",
    [
        "known_numbers: [{number: '+37060000001', name: X, action: teleport}]",
        "known_numbers: [{number: 'call me', name: X}]",
        "keyword_rules: [{name: courier, keywords: []}]",
        "surprise_key: 1",
    ],
)
def test_invalid_rules_rejected(tmp_path: Path, bad: str) -> None:
    path = tmp_path / "rules.yaml"
    path.write_text(bad, encoding="utf-8")
    with pytest.raises(ValidationError):
        RulesEngine.from_yaml(path)


def test_example_rules_file_is_valid() -> None:
    engine = RulesEngine.from_yaml(EXAMPLE)
    assert {r.name for r in engine.config.keyword_rules} >= {"courier", "sales"}
    assert [r.name for r in engine.match_keywords("esu kurjeris iš DPD")] == ["courier"]
