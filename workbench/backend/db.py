"""SQLite 初始化与连接管理。

**重要：SQLite 在本项目中只是索引/缓存，不是事实源。**
事实源是磁盘上的 Markdown（`projects/{书名}/`）与 `.workbench/` 下的运行时文件。
因此本库随时可以整体删除并重建：`init_db()` 会幂等地重建全部表，
任何写入 SQLite 的数据都必须能够从磁盘重新推导出来。

索引类表（projects/chapters/index_docs/characters/...）允许清空重建；
流程类表（tasks/proposals/reviews/quality_debts/workflow_runs/chat_*）记录的是
「过程」，其事实源是 `.workbench/` 下的任务产物与项目内 Markdown。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import config

# 表清单（验收用：全部就绪才算 DB ready）
TABLE_NAMES: tuple[str, ...] = (
    # M0/M1
    "projects",
    "chapters",
    "templates",
    "snapshots",
    "operation_log",
    # M2 生成管线
    "tasks",
    "proposals",
    "chapter_contracts",
    "prompt_registry",
    "providers",
    "token_usage",
    "index_docs",
    "embeddings",
    # M3 审稿
    "reviews",
    "quality_debts",
    "style_fingerprints",
    # M4/M6 编排与对话
    "skills",
    "agents",
    "rules",
    "workflows",
    "workflow_runs",
    "chat_sessions",
    "chat_messages",
    "chat_runs",
    "chat_run_events",
    # M7 角色成长
    "characters",
    "character_states",
    "character_memory",
    "timeline_events",
    "foreshadows",
    "resource_ledger",
    # 通用资产
    "assets",
    # 生图工坊
    "image_providers",
    "image_jobs",
    "image_records",
)

_SCHEMA_SQL = """
-- ───────────────────────── M1 数据层 ─────────────────────────

-- 小说项目（索引层：事实源为 projects/{书名}/ 目录）
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    path        TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    archived    INTEGER NOT NULL DEFAULT 0
);

-- 章节索引
CREATE TABLE IF NOT EXISTS chapters (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL,
    rel_path    TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'draft',
    word_count  INTEGER NOT NULL DEFAULT 0,
    hash        TEXT,
    mtime       REAL,
    contract_id TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (project_id, rel_path)
);

-- 开书模板（内置模板 is_builtin=1，不可删除）
CREATE TABLE IF NOT EXISTS templates (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    path       TEXT,
    is_builtin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (name)
);

-- 版本快照（保存/应用类操作产生）
CREATE TABLE IF NOT EXISTS snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER,
    rel_path      TEXT,
    snapshot_path TEXT,
    reason        TEXT,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- 操作日志（删除去向、索引重建等，供 UI 提示与审计）
CREATE TABLE IF NOT EXISTS operation_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER,
    action     TEXT,
    rel_path   TEXT,
    detail     TEXT,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- ───────────────────────── M2 生成管线 ─────────────────────────

-- 生成任务（含 Prompt 版本与上下文快照，可追溯）
CREATE TABLE IF NOT EXISTS tasks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id       INTEGER,
    task_type        TEXT,
    engine           TEXT,
    model            TEXT,
    provider         TEXT,
    agent            TEXT,
    status           TEXT    NOT NULL DEFAULT 'pending',
    prompt_id        TEXT,
    prompt_version   TEXT,
    context_snapshot TEXT,
    result           TEXT,
    error            TEXT,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    finished_at      TEXT
);

-- AI 写入类产出收件箱
CREATE TABLE IF NOT EXISTS proposals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER,
    task_id     INTEGER,
    kind        TEXT,
    title       TEXT,
    target_path TEXT,
    status      TEXT    NOT NULL DEFAULT 'pending',
    payload     TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    applied_at  TEXT
);

-- 章节合同（确认后冻结：status=draft/frozen）
CREATE TABLE IF NOT EXISTS chapter_contracts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER,
    chapter_id  INTEGER,
    rel_path    TEXT,
    status      TEXT    NOT NULL DEFAULT 'draft',
    plot_points TEXT,
    word_budget INTEGER,
    hook_type   TEXT,
    entities    TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    frozen_at   TEXT,
    UNIQUE (project_id, rel_path)
);

-- Prompt 注册表（可追溯版本）
CREATE TABLE IF NOT EXISTS prompt_registry (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt_id      TEXT    NOT NULL,
    version        TEXT    NOT NULL,
    task_type      TEXT,
    context_policy TEXT,
    body           TEXT,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (prompt_id, version)
);

-- 模型供应商配置（凭据本体不入库，仅存引用）
-- 模型列表存 models（JSON 数组）；model_id 为历史遗留列，仅供旧库回退读取，新写入不再使用
CREATE TABLE IF NOT EXISTS providers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id  TEXT    NOT NULL,
    display_name TEXT,
    source       TEXT    NOT NULL DEFAULT 'workbench_custom',
    base_url     TEXT,
    model_id     TEXT,
    models       TEXT,
    capabilities TEXT,
    secret_ref   TEXT,
    timeout_seconds INTEGER DEFAULT 180,
    retry_policy TEXT,
    enabled      INTEGER NOT NULL DEFAULT 0,
    health       TEXT,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (provider_id)
);

-- Token 用量
CREATE TABLE IF NOT EXISTS token_usage (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id        INTEGER,
    task_id           INTEGER,
    provider          TEXT,
    model             TEXT,
    task_type         TEXT,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    cost_estimate     REAL    NOT NULL DEFAULT 0,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- 文档检索索引（FTS 的词表来源；空文件不入索引）
CREATE TABLE IF NOT EXISTS index_docs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER,
    kind       TEXT    NOT NULL,
    rel_path   TEXT    NOT NULL,
    hash       TEXT,
    mtime      REAL,
    word_count INTEGER NOT NULL DEFAULT 0,
    indexed_at TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (project_id, rel_path)
);

-- 向量（本地嵌入；方案为轻量存 BLOB，可整体重建）
CREATE TABLE IF NOT EXISTS embeddings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER,
    doc_id     INTEGER,
    chunk_no   INTEGER NOT NULL DEFAULT 0,
    model      TEXT,
    dim        INTEGER,
    vector     BLOB,
    text       TEXT,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- ───────────────────────── M3 审稿 ─────────────────────────

-- 审稿结果（逐项：已完成/未完成/待核实）
CREATE TABLE IF NOT EXISTS reviews (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER,
    chapter_id INTEGER,
    rel_path   TEXT,
    kind       TEXT    NOT NULL DEFAULT 'contract',
    verdict    TEXT,
    payload    TEXT,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- 质量债（债-1 / 债-2 / 阻断）
CREATE TABLE IF NOT EXISTS quality_debts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER,
    chapter_id INTEGER,
    rel_path   TEXT,
    level      TEXT,
    status     TEXT    NOT NULL DEFAULT 'open',
    note       TEXT,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- 文风指纹（用户认可章节采样）
CREATE TABLE IF NOT EXISTS style_fingerprints (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER,
    rel_path   TEXT,
    approved   INTEGER NOT NULL DEFAULT 0,
    payload    TEXT,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- ───────────────────────── M4 / M6 编排与对话 ─────────────────────────

-- 技能（来源：builtin / workbench_custom / imported）
CREATE TABLE IF NOT EXISTS skills (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    description TEXT,
    source      TEXT    NOT NULL DEFAULT 'workbench_custom',
    path        TEXT,
    enabled     INTEGER NOT NULL DEFAULT 1,
    size_bytes  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (name)
);

-- Agent 定义（内置 Agent is_builtin=1，不可删除）
CREATE TABLE IF NOT EXISTS agents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    description  TEXT,
    system_prompt TEXT,
    skills       TEXT,
    provider_id  TEXT,
    model_id     TEXT,
    tools        TEXT,
    params       TEXT,
    is_builtin   INTEGER NOT NULL DEFAULT 0,
    enabled      INTEGER NOT NULL DEFAULT 1,
    version      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (name)
);

-- 规则（scope：global / project；优先级链解析用）
CREATE TABLE IF NOT EXISTS rules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    scope      TEXT    NOT NULL DEFAULT 'global',
    project_id INTEGER,
    name       TEXT    NOT NULL,
    body       TEXT,
    priority   INTEGER NOT NULL DEFAULT 100,
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- 工作流定义
CREATE TABLE IF NOT EXISTS workflows (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    definition TEXT,
    is_builtin INTEGER NOT NULL DEFAULT 0,
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (name)
);

-- 工作流运行（自建检查点，支持暂停/恢复/断点续跑）
CREATE TABLE IF NOT EXISTS workflow_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id INTEGER,
    project_id  INTEGER,
    status      TEXT    NOT NULL DEFAULT 'pending',
    cursor      INTEGER NOT NULL DEFAULT 0,
    payload     TEXT,
    log         TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT
);

-- 对话会话
CREATE TABLE IF NOT EXISTS chat_sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   INTEGER,
    title        TEXT,
    agent        TEXT,
    provider_id  TEXT,
    model_id     TEXT,
    auto_apply   INTEGER NOT NULL DEFAULT 0,   -- 直写开关：1=写工具直接落盘（写前存快照）
    agent_pinned INTEGER NOT NULL DEFAULT 0,   -- 1=作者手选 Agent；0=按意图自动路由
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT
);

-- 对话消息（每轮 = 一次引擎调用 + 上下文包）
CREATE TABLE IF NOT EXISTS chat_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    role       TEXT    NOT NULL,
    content    TEXT,
    meta       TEXT,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- 对话执行与可续接事件（与浏览器连接的生命周期独立）
CREATE TABLE IF NOT EXISTS chat_runs (
    id TEXT PRIMARY KEY,
    session_id INTEGER NOT NULL,
    project_id INTEGER,
    client_request_id TEXT NOT NULL,
    request TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    text TEXT NOT NULL DEFAULT '',
    error_code TEXT,
    error_message TEXT,
    task_id INTEGER,
    user_message_id INTEGER,
    assistant_message_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    UNIQUE(session_id, client_request_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS chat_one_active_run ON chat_runs(session_id)
    WHERE status IN ('queued', 'running', 'cancelling');
CREATE TABLE IF NOT EXISTS chat_run_events (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY(run_id, seq)
);

-- ───────────────────────── M7 角色成长 ─────────────────────────

-- 角色卡索引（事实源为 设定/人物设定.md）
CREATE TABLE IF NOT EXISTS characters (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER,
    name       TEXT    NOT NULL,
    card       TEXT,
    source     TEXT    NOT NULL DEFAULT 'unknown',
    first_seen TEXT,
    updated_at TEXT,
    UNIQUE (project_id, name)
);

-- 角色状态机（字段级版本与来源）
CREATE TABLE IF NOT EXISTS character_states (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER,
    character  TEXT    NOT NULL,
    field      TEXT    NOT NULL,
    value      TEXT,
    source     TEXT    NOT NULL DEFAULT 'unknown',
    version    INTEGER NOT NULL DEFAULT 1,
    chapter    TEXT,
    evidence   TEXT,
    updated_at TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (project_id, character, field)
);

-- 角色视角记忆（谁知道什么/何时知道/经何事件）
CREATE TABLE IF NOT EXISTS character_memory (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   INTEGER,
    character    TEXT    NOT NULL,
    know_what    TEXT,
    when_known   TEXT,
    source_event TEXT,
    source       TEXT    NOT NULL DEFAULT 'unknown',
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- 时间线（章节|故事内时间|事件|依据原文）
CREATE TABLE IF NOT EXISTS timeline_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER,
    chapter    TEXT,
    story_time TEXT,
    event      TEXT,
    evidence   TEXT,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- 伏笔台账（埋设章/内容/回收状态/计划回收章）
CREATE TABLE IF NOT EXISTS foreshadows (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id     INTEGER,
    code           TEXT,
    planted_in     TEXT,
    content        TEXT,
    payoff_status  TEXT    NOT NULL DEFAULT 'open',
    planned_payoff TEXT,
    evidence       TEXT,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- 资源账本（数值表 + 算术校验）
CREATE TABLE IF NOT EXISTS resource_ledger (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER,
    chapter    TEXT,
    item       TEXT,
    delta      REAL,
    balance    REAL,
    unit       TEXT,
    evidence   TEXT,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- ───────────────────────── 通用资产 ─────────────────────────

-- 资产（设定卡片 / 拆书事实卡等）
CREATE TABLE IF NOT EXISTS assets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER,
    kind       TEXT,
    name       TEXT,
    path       TEXT,
    meta       TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ───────────────────────── 生图工坊 ─────────────────────────

-- 图片模型供应商（凭据不入库，仅存 secret_ref；不投影 dsh）
CREATE TABLE IF NOT EXISTS image_providers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id     TEXT    NOT NULL,
    display_name    TEXT,
    model_id        TEXT    NOT NULL,
    base_url        TEXT    NOT NULL,
    secret_ref      TEXT,
    timeout_seconds INTEGER NOT NULL DEFAULT 600,
    enabled         INTEGER NOT NULL DEFAULT 0,
    health          TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (provider_id)
);

-- 图片生成任务（过程记录；一次 API 调用，产出 n 张图）
CREATE TABLE IF NOT EXISTS image_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id TEXT    NOT NULL,
    model_id    TEXT    NOT NULL,
    mode        TEXT    NOT NULL DEFAULT 'generate',
    prompt      TEXT    NOT NULL,
    params      TEXT,
    status      TEXT    NOT NULL DEFAULT 'running',
    error       TEXT,
    duration_ms INTEGER,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT
);

-- 图片记录（画廊条目；事实源 = 磁盘图片 + sidecar JSON，可由 reindex 重建）
CREATE TABLE IF NOT EXISTS image_records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER,
    provider_id TEXT,
    model_id    TEXT,
    prompt      TEXT,
    params      TEXT,
    file_rel    TEXT NOT NULL,
    size        TEXT,
    favorite    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""

# 旧库补列（历史库文件可能缺列；SQLite 索引可重建，这里只做最小补齐）
_MIGRATIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "tasks": (
        ("provider", "TEXT"),
        ("agent", "TEXT"),
        ("prompt_id", "TEXT"),
        ("prompt_version", "TEXT"),
        ("context_snapshot", "TEXT"),
        ("result", "TEXT"),
    ),
    "chapters": (("contract_id", "TEXT"),),
    "providers": (
        ("source", "TEXT DEFAULT 'workbench_custom'"),
        ("secret_ref", "TEXT"),
        ("timeout_seconds", "INTEGER DEFAULT 180"),
        ("retry_policy", "TEXT"),
        ("health", "TEXT"),
        ("models", "TEXT"),
    ),
    "proposals": (("title", "TEXT"), ("applied_at", "TEXT")),
    "reviews": (("rel_path", "TEXT"), ("kind", "TEXT DEFAULT 'contract'")),
    "quality_debts": (("rel_path", "TEXT"),),
    "snapshots": (("reason", "TEXT"),),
    "skills": (("description", "TEXT"), ("size_bytes", "INTEGER DEFAULT 0")),
    "workflows": (("is_builtin", "INTEGER DEFAULT 0"),),
    "chapter_contracts": (("rel_path", "TEXT"), ("frozen_at", "TEXT")),
    "token_usage": (("task_type", "TEXT"), ("cost_estimate", "REAL DEFAULT 0")),
    "chat_sessions": (
        ("auto_apply", "INTEGER NOT NULL DEFAULT 0"),
        ("agent_pinned", "INTEGER NOT NULL DEFAULT 0"),
        ("mode", "TEXT NOT NULL DEFAULT 'write'"),
        ("dsh_session_id", "TEXT NOT NULL DEFAULT ''"),
        ("dsh_ready", "INTEGER NOT NULL DEFAULT 0"),
        ("permission_mode", "TEXT NOT NULL DEFAULT ''"),
        ("discussion_only", "INTEGER NOT NULL DEFAULT 1"),
        ("title_source", "TEXT NOT NULL DEFAULT ''"),
        ("title_status", "TEXT NOT NULL DEFAULT 'idle'"),
        ("title_job_id", "TEXT"),
    ),
    "chat_messages": (("dsh_delivered", "INTEGER NOT NULL DEFAULT 0"),),
}


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """补齐缺失列（幂等；失败不阻断启动——索引可整体重建）。"""
    for table, columns in _MIGRATIONS.items():
        existing = _table_columns(conn, table)
        if not existing:
            continue
        for name, decl in columns:
            if name not in existing:
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                except sqlite3.Error:
                    pass
    # Empty is the migration sentinel. Existing titles are conservatively manual;
    # legacy direct-write sessions become auto, never full access.
    conn.execute("UPDATE chat_sessions SET permission_mode=CASE"
                 " WHEN mode='write' AND auto_apply=1 THEN 'auto' ELSE 'ask' END,"
                 " discussion_only=CASE WHEN mode='write' AND auto_apply IN (0,1) THEN 0 ELSE 1 END"
                 " WHERE permission_mode='' OR permission_mode IS NULL")
    conn.execute("UPDATE chat_sessions SET title_source=CASE WHEN title IS NULL OR title='' OR title='新对话'"
                 " THEN 'default' ELSE 'user' END WHERE title_source='' OR title_source IS NULL")
    conn.execute("DROP INDEX IF EXISTS chat_one_active_run")
    conn.execute("CREATE UNIQUE INDEX chat_one_active_run ON chat_runs(session_id)"
                 " WHERE status IN ('queued','running','waiting_input','cancelling')")


def _init_fts(conn: sqlite3.Connection) -> bool:
    """建立 FTS5 全文索引表（不可用时返回 False，检索降级为 LIKE）。"""
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5("
            "project_id UNINDEXED, rel_path UNINDEXED, kind UNINDEXED, body, "
            "tokenize = 'unicode61')"
        )
        return True
    except sqlite3.Error:
        return False


def fts_available(conn: sqlite3.Connection | None = None) -> bool:
    """当前库是否具备 FTS5 表。"""
    owns = conn is None
    if owns:
        conn = sqlite3.connect(config.DB_PATH)
        conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='docs_fts'"
        ).fetchall()
        return bool(rows)
    finally:
        if owns and conn is not None:
            conn.close()


@contextmanager
def get_conn(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """打开 SQLite 连接：正常退出提交，异常回滚，最后关闭。"""
    path = Path(db_path) if db_path is not None else config.DB_PATH
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path | None = None) -> Path:
    """幂等建表（CREATE TABLE IF NOT EXISTS + 补列），返回实际使用的库文件路径。"""
    path = Path(db_path) if db_path is not None else config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn(path) as conn:
        conn.executescript(_SCHEMA_SQL)
        _apply_migrations(conn)
        _init_fts(conn)
    return path


def is_ready(db_path: Path | None = None) -> bool:
    """库文件存在且全部业务表齐备时返回 True。"""
    path = Path(db_path) if db_path is not None else config.DB_PATH
    if not path.exists():
        return False
    with get_conn(path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    existing = {row["name"] for row in rows}
    return set(TABLE_NAMES).issubset(existing)
