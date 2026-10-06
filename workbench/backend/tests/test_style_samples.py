"""Current style authorization: migration, cancellation, versions and races."""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from workbench.backend import config, db
from workbench.backend.api import writing
from workbench.backend.services import project_service, style_service
from workbench.backend.services.errors import InvalidOperationError, ServiceError


@pytest.fixture()
def book(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(config, 'PROJECTS_DIR', tmp_path / 'books')
    monkeypatch.setattr(config, 'RUNTIME_DIR', tmp_path / '.workbench')
    monkeypatch.setattr(config, 'DB_PATH', tmp_path / '.workbench' / 'test.db')
    db.init_db()
    config.ensure_runtime_dirs()
    project = project_service.create_project(name='样稿授权测试')
    _, root = project_service.get_project_dir(project['id'])
    rel = '章节/第0001章.txt'
    (root / rel).write_text('沈砚合上账册。“这枚铜钱归你。”他把灯往柜台推。', encoding='utf-8')
    app = FastAPI()
    app.include_router(writing.router)
    @app.exception_handler(ServiceError)
    async def service_error(_request, error):
        return JSONResponse(status_code=error.status_code, content={'detail': error.message})
    return SimpleNamespace(id=project['id'], root=root, rel=rel, client=TestClient(app))


def test_repeat_sampling_is_one_weight_preserves_omitted_note_and_explicit_clear(book):
    first = style_service.sample(book.id, book.rel, note='保留人物目的之间的冲突。')
    again = style_service.sample(book.id, book.rel)
    assert first['id'] == again['id']
    assert again['note'] == first['note'] and again['usable'] and again['created_at']
    assert len(style_service.list_fingerprints(book.id)) == 1
    assert style_service.reference_metrics(book.id)['samples'] == 1
    assert style_service.sample(book.id, book.rel, note='')['note'] == ''


def test_concurrent_sampling_has_one_authorization(book):
    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(lambda _: style_service.sample(book.id, book.rel), range(12)))
    assert len({row['id'] for row in rows}) == 1
    assert style_service.reference_metrics(book.id)['samples'] == 1


def test_cancel_is_project_scoped_and_does_not_read_missing_source(book):
    sample = style_service.sample(book.id, book.rel)
    other = project_service.create_project(name='另一书')
    response = book.client.patch(f'/api/projects/{other["id"]}/style/fingerprints/{sample["id"]}', json={'approved': False})
    assert response.status_code == 404
    assert style_service.reference_metrics(book.id)['samples'] == 1
    (book.root / book.rel).unlink()
    response = book.client.patch(f'/api/projects/{book.id}/style/fingerprints/{sample["id"]}', json={'approved': False})
    assert response.status_code == 200 and response.json()['reference_status'] == 'unapproved'
    assert style_service.reference_metrics(book.id) is None
    assert style_service.generation_reference(book.id)['samples'] == []
    assert book.client.patch(f'/api/projects/{book.id}/style/fingerprints/{sample["id"]}', json={'approved': True}).status_code == 422


def test_new_revision_supersedes_old_approval_and_cannot_revive(book):
    original = (book.root / book.rel).read_text(encoding='utf-8')
    first = style_service.sample(book.id, book.rel)
    (book.root / book.rel).write_text('新稿。“先查清账，再谈铜钱。”', encoding='utf-8')
    assert not style_service.list_fingerprints(book.id)[0]['usable']
    second = style_service.sample(book.id, book.rel)
    assert first['id'] == second['id'] and first['content_hash'] != second['content_hash']
    (book.root / book.rel).write_text(original, encoding='utf-8')
    assert style_service.reference_metrics(book.id) is None
    style_service.cancel_approval(book.id, second['id'])
    assert not style_service.list_fingerprints(book.id)[0]['approved']


def test_sample_api_preserves_note_when_omitted_and_returns_validity(book):
    prefix = f'/api/projects/{book.id}/style/sample'
    first = book.client.post(prefix, json={'rel_path': book.rel, 'note': '对白留有信息差。'}).json()
    second = book.client.post(prefix, json={'rel_path': book.rel}).json()
    assert first['id'] == second['id'] and second['note'] == first['note']
    assert second['usable'] and second['reference_status'] == 'ready'
    assert book.client.post(prefix, json={'rel_path': book.rel, 'note': ''}).json()['note'] == ''


def test_empty_sample_cannot_be_authorized(book):
    (book.root / book.rel).write_text(' \n', encoding='utf-8')
    with pytest.raises(InvalidOperationError):
        style_service.sample(book.id, book.rel)
    assert style_service.list_fingerprints(book.id) == []


def test_migration_archives_originals_canonicalizes_and_latest_cancel_wins(book):
    payload = json.dumps({'content_hash': 'a' * 64, 'note': '旧作者备注'}, ensure_ascii=False)
    with db.get_conn() as conn:
        conn.execute('DROP INDEX style_one_chapter')
        for rel, approved, data in [(book.rel, 1, payload), ('章节/第0001章.md', 0, payload),
                                   ('章节\\第0002章.txt', 1, payload), ('../他书.txt', 1, payload)]:
            conn.execute('INSERT INTO style_fingerprints(project_id,rel_path,approved,payload) VALUES (?,?,?,?)',
                         (book.id, rel, approved, data))
    db.init_db()
    with db.get_conn() as conn:
        rows = conn.execute('SELECT * FROM style_fingerprints ORDER BY id').fetchall()
        archived = conn.execute('SELECT * FROM style_fingerprints_archive').fetchall()
    assert len(rows) == 2 and rows[0]['rel_path'] == book.rel and rows[0]['approved'] == 0
    assert json.loads(rows[0]['payload'])['requires_resample'] is True
    assert {row['reason'] for row in archived} == {'legacy_chapter', 'duplicate_chapter', 'normalized_path', 'invalid_source'}
    assert all(row['payload'] == payload for row in archived)
    db.init_db()
    with db.get_conn() as conn:
        assert conn.execute('SELECT count(*) FROM style_fingerprints_archive').fetchone()[0] == len(archived)
    assert style_service.sample(book.id, book.rel)['id'] == rows[0]['id']
    assert style_service.list_fingerprints(book.id)[-1]['reference_status'] == 'ready'


def test_migration_failure_rolls_back_archive_and_current_rows(book):
    with db.get_conn() as conn:
        conn.execute('DROP INDEX style_one_chapter')
        conn.execute("INSERT INTO style_fingerprints(project_id,rel_path,payload) VALUES (?,?,'{}')", (book.id, book.rel))
        conn.execute("INSERT INTO style_fingerprints(project_id,rel_path,payload) VALUES (?,?,'{}')", (book.id, book.rel))
        conn.execute("CREATE TRIGGER reject_style_dedupe BEFORE DELETE ON style_fingerprints BEGIN SELECT RAISE(ABORT,'test migration failure'); END")
    with pytest.raises(Exception, match='test migration failure'):
        db.init_db()
    with db.get_conn() as conn:
        assert conn.execute('SELECT count(*) FROM style_fingerprints').fetchone()[0] == 2
        assert conn.execute('SELECT count(*) FROM style_fingerprints_archive').fetchone()[0] == 0


def test_more_than_one_hundred_records_do_not_hide_old_valid_samples(book):
    body = (book.root / book.rel).read_text(encoding='utf-8')
    payload = {**style_service.compute_fingerprint(body), 'content_hash': hashlib.sha256(body.encode()).hexdigest()}
    with db.get_conn() as conn:
        for number in range(1, 103):
            rel = f'章节/第{number:04d}章.txt'
            (book.root / rel).write_text(body if number == 1 else '已变稿。', encoding='utf-8')
            conn.execute('INSERT INTO style_fingerprints(project_id,rel_path,approved,payload) VALUES (?,?,1,?)',
                         (book.id, rel, json.dumps(payload)))
    assert len(style_service.list_fingerprints(book.id)) == 102
    assert style_service.reference_metrics(book.id)['samples'] == 1
    assert {sample['rel_path'] for sample in style_service.generation_reference(book.id)['samples']} == {book.rel}
