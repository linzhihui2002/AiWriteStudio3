"""No network: model contracts, long-tail coverage and fail-closed spaces."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from workbench.backend import config
from workbench.backend.services import embedding_service as embeddings
from workbench.backend.services.errors import InvalidOperationError


def test_missing_model_is_explicit_lexical_degradation(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path)
    engine = embeddings.resolve_embedder()
    assert engine.name == embeddings.MODEL_ID
    assert engine.info()["offline"] is True
    assert engine.ready is False
    with pytest.raises(InvalidOperationError, match="未就绪"):
        engine.embed_query("人物受伤")


def test_long_single_paragraph_covers_end_and_preserves_anchors():
    text = "---\n标题: 设定\n---\n" + "甲乙丙丁" * 1200 + "最后交出了青铜鱼符。"
    chunks = embeddings.chunk_document(text)
    assert len(chunks) > 10
    assert "最后交出了青铜鱼符。" in chunks[-1]["text"]
    assert all(text[chunk["start"]:chunk["end"]] == chunk["text"] for chunk in chunks)
    assert all(b["start"] < a["end"] for a, b in zip(chunks, chunks[1:]))
    assert all(chunk["line_start"] == 4 for chunk in chunks)


def test_onnx_all_inputs_cls_normalization_and_query_prefix(monkeypatch):
    np = pytest.importorskip("numpy")
    calls = []

    class Tokenizer:
        def enable_truncation(self, **kwargs):
            assert kwargs["max_length"] == 512

        def enable_padding(self, **kwargs):
            pass

        def encode_batch(self, texts):
            calls.extend(texts)
            return [SimpleNamespace(ids=[1, 2], attention_mask=[1, 1], type_ids=[0, 0]) for _ in texts]

    class Session:
        def get_inputs(self):
            return [SimpleNamespace(name=name) for name in ("input_ids", "attention_mask", "token_type_ids")]

        def run(self, _, feeds):
            assert set(feeds) == {"input_ids", "attention_mask", "token_type_ids"}
            assert all(value.dtype == np.int64 for value in feeds.values())
            output = np.ones((len(feeds["input_ids"]), 2, 512))
            output[:, 0, 0] = 20
            output[:, 1, :] = 900  # Mean pooling would make the vector uniform.
            return [output]

    engine = embeddings.Embedder(mode="lexical")
    engine.mode, engine.ready = "local", True
    engine._session = Session()
    monkeypatch.setattr(engine, "_load", lambda: None)
    monkeypatch.setattr(engine, "tokenizer", lambda: Tokenizer())
    vector = engine.embed_query("鱼符")
    documents = engine.embed_documents(["把鱼符交给老周"])
    assert calls == [embeddings.QUERY_PREFIX + "鱼符", "把鱼符交给老周"]
    assert vector[0] > vector[1] * 10
    assert sum(value * value for value in vector) == pytest.approx(1)
    assert len(documents[0]) == 512


def test_cloud_does_not_fall_back_to_hashing(tmp_path, monkeypatch):
    from workbench.backend import db
    from workbench.backend.services import generation_service
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "workbench.db")
    db.init_db()
    monkeypatch.setattr("workbench.backend.services.provider_service.get_provider",
                        lambda *_: {"enabled": True, "base_url": "https://fake.invalid/v1"})
    monkeypatch.setattr("workbench.backend.engine.direct_api.DirectApiEngine.embed",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")))
    engine = embeddings.Embedder(provider="fake", model="embedding-test", mode="cloud")
    with pytest.raises(InvalidOperationError, match="云嵌入失败"):
        engine.embed_query("鱼符")
    assert engine.name == "cloud:fake:embedding-test"
    with db.get_conn() as conn:
        assert conn.execute("SELECT status FROM tasks").fetchone()["status"] == "failed"


def test_corrupt_local_package_is_rejected_before_inference(tmp_path, monkeypatch):
    import json
    import hashlib
    model = tmp_path / "bge"
    model.mkdir()
    expected = hashlib.sha256(b"verified").hexdigest()
    monkeypatch.setattr(embeddings, "FILES", {"model_optimized.onnx": expected})
    (model / "model_optimized.onnx").write_bytes(b"tampered")
    (model / "manifest.json").write_text(json.dumps({
        "model": embeddings.MODEL_ID, "revision": embeddings.REVISION}), encoding="utf-8")
    engine = embeddings.Embedder(model_dir=str(model), mode="local")
    assert not engine.ready
    assert "校验失败" in engine.error
    with pytest.raises(InvalidOperationError):
        engine.embed_query("旧港鱼符")


def test_failed_model_download_leaves_lexical_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / ".workbench")
    monkeypatch.setattr("workbench.backend.services.embedding_service.httpx.stream",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    state = embeddings.prepare(background=False, enable=False)
    assert state["status"] == "failed"
    assert "offline" in state["error"]
    assert not embeddings.Embedder(model_dir=str(embeddings.model_directory()), mode="local").ready
    assert not (embeddings.model_directory() / "manifest.json").exists()
