"""生图工坊单测：尺寸计算 / 适配器 / 供应商 / 任务生命周期 / 记录与暂存。

单测**绝不触网**：httpx 调用全部用 fake response + monkeypatch（同 test_engine）。
"""

from __future__ import annotations

import base64
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from workbench.backend import config, db
from workbench.backend.services import (
    image_provider_service,
    image_service,
)
from workbench.backend.services.errors import InvalidOperationError
from workbench.backend.services.image_adapters import (
    GPTImageAdapter,
    ImageGenRequest,
    calc_size,
    parse_size,
    resolve_adapter,
)


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    runtime = tmp_path / ".workbench"
    runtime.mkdir()
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(config, "DB_PATH", runtime / "workbench.db")
    db.init_db()
    config.ensure_runtime_dirs()
    return SimpleNamespace(runtime=runtime, root=tmp_path)


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | str) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = payload if isinstance(payload, str) else "{}"

    def json(self) -> dict:
        assert isinstance(self._payload, dict)
        return self._payload


PNG_BYTES = b"\x89PNG-fake-image-bytes"
PNG_B64 = base64.b64encode(PNG_BYTES).decode("ascii")


def _make_provider(provider_id: str = "imgprov", *, enabled: bool = True) -> dict:
    return image_provider_service.upsert_provider(
        provider_id,
        display_name="测试图片供应商",
        model_id="gpt-image-2",
        base_url="https://api.example.com/v1",
        api_key="sk-test-key-123456",
        enabled=enabled,
    )


# ─────────────────────────── 尺寸计算 ───────────────────────────


def test_calc_size_matches_reference_site() -> None:
    assert calc_size("2K", "2:3") == "1440x2160"
    assert calc_size("1K", "2:3") == "1024x1536"
    assert calc_size("1K", "1:1") == "1024x1024"
    assert calc_size("2K", "3:2") == "2160x1440"
    assert calc_size("2K", "16:9") == "2560x1440"
    assert calc_size("2K", "9:16") == "1440x2560"
    assert calc_size("4K", "2:3") == "2048x3072"


def test_calc_size_custom_ratio_and_errors() -> None:
    assert calc_size("1K", "7:5") == "1440x1024"
    with pytest.raises(InvalidOperationError):
        calc_size("8K", "1:1")  # 基准分辨率不支持
    with pytest.raises(InvalidOperationError):
        calc_size("1K", "abc")
    with pytest.raises(InvalidOperationError):
        calc_size("1K", "2:0")


def test_parse_size_rejects_bad_input() -> None:
    assert parse_size("1440x2160") == (1440, 2160)
    with pytest.raises(InvalidOperationError):
        parse_size("wide")
    with pytest.raises(InvalidOperationError):
        parse_size("0x100")


# ─────────────────────────── 适配器 ───────────────────────────


def test_resolve_adapter_matches_gpt_image_family() -> None:
    assert resolve_adapter("gpt-image-2").key == "gpt-image"
    assert resolve_adapter("GPT-Image-1").key == "gpt-image"
    # 未知模型回落 OpenAI 兼容兜底
    assert resolve_adapter("seedream-x").key == "gpt-image"


def test_generations_payload_complete() -> None:
    adapter = GPTImageAdapter()
    req = ImageGenRequest(
        prompt="封面图", model_id="gpt-image-2", size="1440x2160",
        quality="high", output_format="png", background="auto",
        moderation="auto", n=2,
    )
    url, payload, parts = adapter.build_request("https://api.example.com/v1/", req)
    assert url == "https://api.example.com/v1/images/generations"
    assert parts is None
    assert payload == {
        "model": "gpt-image-2", "prompt": "封面图", "n": 2,
        "size": "1440x2160", "quality": "high", "output_format": "png",
        "background": "auto", "moderation": "auto",
    }


def test_edits_request_is_multipart() -> None:
    adapter = GPTImageAdapter()
    req = ImageGenRequest(
        prompt="改成夜景", model_id="gpt-image-2", size="1024x1024",
        output_format="png", background="auto", moderation="auto", n=1,
        input_images=[("ref.png", PNG_BYTES)],
    )
    url, payload, parts = adapter.build_request("https://api.example.com/v1", req)
    assert url == "https://api.example.com/v1/images/edits"
    assert payload is None and parts is not None
    image_parts = [p for p in parts if p["name"] == "image[]"]
    data_parts = [p for p in parts if p["name"] == "__data__"]
    assert len(image_parts) == 1 and image_parts[0]["content"] == PNG_BYTES
    assert data_parts and data_parts[0]["data"]["prompt"] == "改成夜景"


def test_parse_response_prefers_b64_and_supports_url() -> None:
    adapter = GPTImageAdapter()
    results = adapter.parse_response(
        {"data": [{"b64_json": "abc"}, {"url": "https://cdn/x.png"}, {"junk": 1}]}
    )
    assert results == [{"b64": "abc"}, {"url": "https://cdn/x.png"}]


def test_validate_rejects_out_of_capability() -> None:
    adapter = GPTImageAdapter()
    caps = adapter.capabilities()
    with pytest.raises(InvalidOperationError):
        adapter.validate(ImageGenRequest(
            prompt="x", model_id="gpt-image-2", n=caps["max_n"] + 1,
        ))
    with pytest.raises(InvalidOperationError):
        adapter.validate(ImageGenRequest(
            prompt="x", model_id="gpt-image-2", size="999999x999999",
        ))
    with pytest.raises(InvalidOperationError):
        adapter.validate(ImageGenRequest(
            prompt="x", model_id="gpt-image-2", quality="ultra",
        ))


# ─────────────────────────── 供应商 ───────────────────────────


def test_provider_upsert_masks_secret(workspace: SimpleNamespace) -> None:
    provider = _make_provider()
    assert provider["has_secret"] is True
    assert provider["masked_secret"].startswith("sk-te")
    assert "sk-test-key-123456" not in str(provider)
    # 更新时不传 api_key 不丢 Key
    updated = image_provider_service.upsert_provider(
        "imgprov", model_id="gpt-image-2", base_url="https://api.example.com/v1",
        api_key=None, enabled=True,
    )
    assert updated["has_secret"] is True


def test_provider_resolve_requires_key(workspace: SimpleNamespace) -> None:
    image_provider_service.upsert_provider(
        "nokey", model_id="gpt-image-2",
        base_url="https://api.example.com/v1", enabled=True,
    )
    with pytest.raises(InvalidOperationError, match="API Key"):
        image_provider_service.resolve("nokey")


def test_provider_resolve_requires_enabled(workspace: SimpleNamespace) -> None:
    _make_provider("disabled-prov", enabled=False)
    with pytest.raises(InvalidOperationError, match="未启用"):
        image_provider_service.resolve("disabled-prov")


def test_provider_health_unreachable(workspace: SimpleNamespace, monkeypatch) -> None:
    _make_provider()

    def fake_get(url, **kwargs):
        return _FakeResponse(401, {"error": "bad key"})

    monkeypatch.setattr(image_provider_service.httpx, "get", fake_get)
    result = image_provider_service.health_check("imgprov")
    assert result["ok"] is False
    provider = image_provider_service.get_provider("imgprov")
    assert provider["health"] == "unreachable"


def test_provider_health_ok_without_model_match(
    workspace: SimpleNamespace, monkeypatch
) -> None:
    _make_provider()

    def fake_get(url, **kwargs):
        return _FakeResponse(200, {"data": [{"id": "other-model"}]})

    monkeypatch.setattr(image_provider_service.httpx, "get", fake_get)
    result = image_provider_service.health_check("imgprov")
    assert result["ok"] is True  # /models 连通即可，模型不匹配仅提示
    assert result["model_matched"] is False


# ─────────────────────────── 任务生命周期 ───────────────────────────


def test_job_success_saves_records_and_files(
    workspace: SimpleNamespace, monkeypatch
) -> None:
    _make_provider()
    captured: dict = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return _FakeResponse(200, {"data": [{"b64_json": PNG_B64}, {"b64_json": PNG_B64}]})

    monkeypatch.setattr(image_service.httpx, "post", fake_post)
    job = image_service.create_job(
        "imgprov", "一张测试图", size="1440x2160", quality="high", n=2,
    )
    assert job["status"] == "running"
    final = image_service.wait_job(job["id"])
    assert final["status"] == "succeeded", final["error"]
    assert captured["url"] == "https://api.example.com/v1/images/generations"
    assert captured["json"]["model"] == "gpt-image-2"

    records = image_service.list_records()
    assert len(records) == 2
    for record in records:
        assert record["prompt"] == "一张测试图"
        assert record["size"] == "1440x2160"
        path = image_service.record_file_path(record["id"])
        assert path.is_file()
        assert path.with_suffix(".json").is_file()
        assert path.read_bytes() == PNG_BYTES


def test_job_edit_mode_posts_multipart(
    workspace: SimpleNamespace, monkeypatch
) -> None:
    _make_provider()
    upload = image_service.save_upload(PNG_BYTES, "image/png", "参考图.png")
    captured: dict = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["files"] = kwargs.get("files")
        captured["data"] = kwargs.get("data")
        return _FakeResponse(200, {"data": [{"b64_json": PNG_B64}]})

    monkeypatch.setattr(image_service.httpx, "post", fake_post)
    job = image_service.create_job(
        "imgprov", "改成夜景", size="1024x1024",
        input_upload_ids=[upload["upload_id"]],
    )
    final = image_service.wait_job(job["id"])
    assert final["status"] == "succeeded", final["error"]
    assert final["mode"] == "edit"
    assert captured["url"] == "https://api.example.com/v1/images/edits"
    assert any(name == "image[]" for name, _ in captured["files"])
    assert captured["data"]["prompt"] == "改成夜景"
    assert image_service.list_records()[0]["params"]["input_uploads"]


def test_job_failure_records_error(workspace: SimpleNamespace, monkeypatch) -> None:
    _make_provider()

    def fake_post(url, **kwargs):
        return _FakeResponse(401, {"error": "invalid api key"})

    monkeypatch.setattr(image_service.httpx, "post", fake_post)
    job = image_service.create_job("imgprov", "会失败的任务")
    final = image_service.wait_job(job["id"])
    assert final["status"] == "failed"
    assert "401" in final["error"] or "AUTH" in final["error"].upper()
    assert image_service.list_records() == []


def test_cancel_discards_results(workspace: SimpleNamespace, monkeypatch) -> None:
    _make_provider()
    holder: dict = {}
    gate = threading.Event()

    def fake_post(url, **kwargs):
        gate.wait(5)
        image_service.cancel_job(holder["job_id"])
        return _FakeResponse(200, {"data": [{"b64_json": PNG_B64}]})

    monkeypatch.setattr(image_service.httpx, "post", fake_post)
    job = image_service.create_job("imgprov", "将被取消")
    holder["job_id"] = job["id"]
    gate.set()
    final = image_service.wait_job(job["id"])
    assert final["status"] == "cancelled"
    assert image_service.list_records() == []
    # 磁盘上不遗留产物
    images_root = config.images_dir()
    leftovers = [p for p in images_root.rglob("*")
                 if p.is_file() and "_uploads" not in p.parts]
    assert leftovers == []


def test_create_job_requires_prompt(workspace: SimpleNamespace) -> None:
    _make_provider()
    with pytest.raises(InvalidOperationError):
        image_service.create_job("imgprov", "   ")


def test_create_job_rejects_unknown_provider(workspace: SimpleNamespace) -> None:
    with pytest.raises(Exception):
        image_service.create_job("ghost", "提示词")


# ─────────────────────────── 画廊记录 ───────────────────────────


def _seed_record(workspace: SimpleNamespace) -> dict:
    rel = image_service._save_image(
        PNG_BYTES,
        ext=".png",
        meta={
            "job_id": None, "provider_id": "p1", "model_id": "gpt-image-2",
            "prompt": "山间云海", "params": {"size": "1024x1024"},
            "size": "1024x1024",
        },
    )
    with db.get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO image_records"
            " (job_id, provider_id, model_id, prompt, params, file_rel, size)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (None, "p1", "gpt-image-2", "山间云海",
             '{"size": "1024x1024"}', rel, "1024x1024"),
        )
        record_id = int(cursor.lastrowid)
    return image_service.get_record(record_id)


def test_record_favorite_and_search(workspace: SimpleNamespace) -> None:
    record = _seed_record(workspace)
    assert record["favorite"] is False
    patched = image_service.patch_record(record["id"], favorite=True)
    assert patched["favorite"] is True
    assert image_service.list_records(favorite=True)[0]["id"] == record["id"]
    assert image_service.list_records(search="云海")[0]["id"] == record["id"]
    assert image_service.list_records(search="找不到") == []


def test_record_delete_removes_files(workspace: SimpleNamespace) -> None:
    record = _seed_record(workspace)
    path = image_service.record_file_path(record["id"])
    assert path.is_file()
    image_service.delete_record(record["id"])
    assert not path.exists()
    assert not path.with_suffix(".json").exists()
    assert image_service.list_records() == []


def test_reindex_rebuilds_from_disk(workspace: SimpleNamespace) -> None:
    _seed_record(workspace)
    # 清空后 reindex 应从磁盘 sidecar 重建
    with db.get_conn() as conn:
        conn.execute("DELETE FROM image_records")
    assert image_service.list_records() == []
    result = image_service.reindex()
    assert result["rebuilt"] == 1
    records = image_service.list_records()
    assert len(records) == 1
    assert records[0]["prompt"] == "山间云海"
    assert records[0]["favorite"] is False  # 收藏状态不随重建保留（索引可弃）


# ─────────────────────────── 参考图暂存 ───────────────────────────


def test_upload_roundtrip_and_validation(workspace: SimpleNamespace) -> None:
    upload = image_service.save_upload(PNG_BYTES, "image/png", "我的 参考.png")
    filename, content = image_service.load_upload(upload["upload_id"])
    assert content == PNG_BYTES
    assert filename.startswith(upload["upload_id"])

    with pytest.raises(InvalidOperationError):
        image_service.save_upload(PNG_BYTES, "image/gif")
    with pytest.raises(InvalidOperationError):
        image_service.save_upload(b"", "image/png")
    with pytest.raises(InvalidOperationError):
        image_service.save_upload(b"x" * (5 * 1024 * 1024 + 1), "image/png")
    with pytest.raises(Exception):
        image_service.load_upload("nonexistent-id")
