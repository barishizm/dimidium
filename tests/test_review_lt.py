from pathlib import Path

from app.speech.phrases_lt import PHRASES

REVIEW_DOC = Path(__file__).resolve().parents[1] / "docs" / "REVIEW_LT.md"


def test_every_unreviewed_phrase_is_listed_for_native_review() -> None:
    doc = REVIEW_DOC.read_text(encoding="utf-8")
    missing = [p.key for p in PHRASES if not p.reviewed and p.text not in doc]
    assert not missing, f"Add these phrases (exact text) to docs/REVIEW_LT.md: {missing}"
