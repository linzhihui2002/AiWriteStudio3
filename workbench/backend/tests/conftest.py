"""Keep backend tests away from the running workbench's native dsh home.

Provider saves now project immediately, so tests that use a temporary database
must also use a temporary projection and credential directory by default.
Individual native tests may override this with their own isolated home.
"""

from __future__ import annotations

from pathlib import Path
from collections.abc import Iterator

import pytest

from workbench.backend.engine import dsh_paths


@pytest.fixture(autouse=True)
def isolated_dsh_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(dsh_paths, "DSH_HOME_DIR", tmp_path / "dsh-home")
    # Semantic-router tests explicitly install their own fake availability and
    # responses. All other isolated tests must never reach a real paid model.
    from workbench.backend.services import intent_service, knowledge_service
    # Keep the real cleanup callback even when a lifespan test replaces the
    # service's public shutdown function with a stub for its own assertions.
    shutdown_index = knowledge_service.shutdown
    monkeypatch.setattr(intent_service, "_llm_engine_available", lambda: False)
    yield
    # Basic indexing now runs even with vector opt-in disabled. Join its workers
    # before monkeypatch restores production paths or another test's database.
    shutdown_index(wait=True)
