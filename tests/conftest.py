"""pytest 公共夹具：每个用例使用独立临时 SQLite 库，互不污染。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import database


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "test_alert.db"
    monkeypatch.setattr(database, "_DB_PATH", db_path)
    return db_path
