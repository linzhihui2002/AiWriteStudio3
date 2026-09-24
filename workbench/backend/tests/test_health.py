"""/api/health 契约测试。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from workbench.backend.app import VERSION, create_app


def test_health_returns_200_with_expected_fields() -> None:
    app = create_app()
    # with 语句会触发 lifespan：初始化运行时目录与 SQLite
    with TestClient(app) as client:
        response = client.get("/api/health")

    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "ok"
    assert data["version"] == VERSION
    assert data["port"] == 8790
    assert data["db_ready"] is True
    assert set(data) >= {"status", "version", "port", "db_ready"}
