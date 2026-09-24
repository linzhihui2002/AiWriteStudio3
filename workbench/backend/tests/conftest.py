"""Keep backend tests away from the running workbench's native dsh home.

Provider saves now project immediately, so tests that use a temporary database
must also use a temporary projection and credential directory by default.
Individual native tests may override this with their own isolated home.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from workbench.backend.engine import dsh_paths


@pytest.fixture(autouse=True)
def isolated_dsh_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dsh_paths, "DSH_HOME_DIR", tmp_path / "dsh-home")
