from collections.abc import Iterator
from pathlib import Path

import pytest

from app.config import get_settings


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the app at a temp DB and ignore any local .env during tests."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
