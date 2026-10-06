# AI 小说创作工作台 · 项目技术文档

> **面向开发者**的实现级技术文档：说明当前代码的真实结构、模块边界、接口契约、数据流与业务流程，用于「快速定位要改哪一层、哪个文件」。
>
> 与 [开发文档](AI小说创作工作台-开发文档.md) 的分工：开发文档承载**产品定位、需求编号（FR-*）、方案与选型决策**；本文件承载**已落地的代码事实**。两者冲突时，以仓库代码与本文件的「代码事实」为准，并回修开发文档。

| 元信息 | 值 |
|---|---|
| 文档版本 | v1.9（2026-10-06：全量核对代码事实——订正服务层 63 / API 8 / 引擎 9 模块计数，补 `api/knowledge.py` 挂载顺序与 §5.8，补 Knowledge 路由行、`ingestion_service` / `prose_history_service` 模块行与 §5.5 本书级向量模型端点，新增 §0.4 优化前后差异对照） |
| 首次成文 | 2026-09-24 |
| 复核方式 | 逐文件通读 `workbench/backend/`、`workbench/frontend/src/`、`workbench/cli/`、`db.py` 表结构、路由定义与测试脚本；测试数字为本次实测 |
| 适用范围 | 本仓库当前代码快照（`VERSION = 0.1.0`），非历史基线 |
| 事实源优先级 | 代码 > 本文件 > 开发文档 |
| 不覆盖 | 产品需求取舍、第三方合规判断（见对应专题文档） |

---

## 0. 文档导航

### 0.1 文档地图

| 文档 | 定位 | 主要读者 | 何时读 |
|---|---|---|---|
| [README.md](../README.md) | 仓库入口：安装、启动、首用、目录、红线 | 所有人 | 第一次接触项目 |
| [快速启动.md](快速启动.md) | 5 分钟跑通的命令清单 | 使用者 | 想把环境跑起来 |
| **本文件** | **实现级技术架构与模块参考** | 开发者 | 要改代码、加接口、加模块 |
| [AI小说创作工作台-开发文档.md](AI小说创作工作台-开发文档.md) | 产品定位、FR 需求、架构决策、里程碑、风险 | 产品/架构 | 想知道「为什么这么设计」 |
| [compat-matrix.md](compat-matrix.md) | dsh 三项能力实测矩阵与调用协议 | 引擎层开发者 | 改 `engine/` 或升级 dsh 前 |
| [对话体验改造-验收记录.md](对话体验改造-验收记录.md) | 对话改造的验收快照（含当时测试数字） | 开发者 | 追溯对话行为的历史验收 |
| [工作台整体改造-验收记录-2026-10-03.md](工作台整体改造-验收记录-2026-10-03.md) | 对话主入口、全站前端与持久执行的本轮交付及限制 | 开发者/作者 | 复核本轮实现、浏览器矩阵、性能与真实模型观察 |
| [模型配置与浏览器提示优化-验收记录-2026-10-03.md](模型配置与浏览器提示优化-验收记录-2026-10-03.md) | 模型配置布局、凭据误识别和浏览器提示入口的专项回归 | 开发者/作者 | 复核凭据输入、复制、表单校验和模型供应商操作 |
| [工作台功能修复与数据一致性-验收记录-2026-10-04.md](工作台功能修复与数据一致性-验收记录-2026-10-04.md) | 本轮功能修复、迁移、浏览器验收及真实审稿/软审结果 | 开发者/作者 | 复核五张截图问题和相邻数据、异步交互缺陷 |
| [近期对话修改与优化说明-2026-09-24.md](近期对话修改与优化说明-2026-09-24.md) | 近期几轮优化的问题背景与当前口径 | 开发者 | 了解改动来龙去脉 |
| [third-party-notices.md](third-party-notices.md) | 第三方资产引入登记 | 所有人 | 引入任何外部代码/文本前 |
| [调研归档/README.md](调研归档/README.md) | 调研归档规范与索引 | 所有人 | 查历史调研证据 |
| [AGENTS.md](../AGENTS.md) | 隔离红线、意图路由表、硬门禁摘要 | 所有人/AI 会话 | 每次会话开始 |

### 0.2 按任务查（我要改 X → 看哪节）

| 我想做的事 | 先读 | 主要落点文件 |
|---|---|---|
| 加一个 HTTP 接口 | [§5 接口清单](#5-接口清单http-api)、[§2.1 分层](#21-分层与依赖方向) | `workbench/backend/api/*.py` + `src/api/client.ts` |
| 改页面/组件样式或交互 | [§4 前端架构](#4-前端架构) | `workbench/frontend/src/pages/*`、`src/components/*` |
| 改章节命名、新建章节规则、章节字数区间 | [§3.1 项目与文档树](#31-项目与文档树) | `services/chapter_service.py`、`services/writing_preference_service.py`、`lib/chapterName.ts` |
| 改生成流程 / 加断点 | [§7.3 8 步循环](#73-章节生产8-步循环) | `services/pipeline_service.py` |
| 改上下文注入内容或预算 | [§6.6 上下文组装](#66-上下文组装数据流) | `services/context_service.py` |
| 调门禁阈值 / 加禁词 | §2.6 门禁层 | `backend/gates/*.py` |
| 改对话权限 / 批准行为 | [§3.5 对话运行](#35-对话运行权限与恢复) | `services/chat_run_service.py`、`file_change_service.py` |
| 接入新的图片模型 | [§3.12 生图工坊](#312-生图工坊) | `services/image_adapters.py`（`ADAPTERS` 头部插入） |
| 接入新的 LLM 供应商 | [§7.9 模型供应商](#79-模型供应商配置与-dsh-投影) | `services/provider_service.py` |
| 加技能 / 改技能 | [§3.13 编排资产](#313-技能agent规则路由与工作流) | `skills/<name>/SKILL.md`，然后 `POST /api/skills/sync` |
| 加 SQLite 表或字段 | §2.4 数据层 | `backend/db.py`（建表 + `_MIGRATIONS`） |
| 升级 vendor dsh | [compat-matrix.md](compat-matrix.md) | `workbench/cli/setup_dsh.py`、`engine/dsh_paths.py` |
| 改 PC 客户端（隔离/备份/打包/窗口壳） | [§10 PC 客户端](#10-pc-客户端pc-client独立实例形态) | `pc-client/**`（独立目录，导入名 `pc_client.backend`） |
| 改安装包/下载页/签名 | [§11 安装包与下载体验](#11-安装包与下载体验) | `pc-client/installer/**`、`pc-client/build/**`、`pc-client/download/**` |

### 0.3 统一术语（全文一致）

| 术语 | 含义 | 代码对应 |
|---|---|---|
| **事实源** | 权威数据本体，可版本化、可人工编辑 | `projects/{书名}/**/*.md`（章节正文为 `章节/第NNNN章.txt` 纯文本）、`.meta/*.json`、`agents/`、`skills/`、`rules/`、`workflows/`、`templates/` |
| **索引** | 派生数据，删库可重建 | `.workbench/workbench.db`（36 个初始化表，另有按需表）、`.workbench/vector/books/<knowledge_key>/vectors.db`（每书独立） |
| **运行态** | 可随时删除重建的执行痕迹 | `.workbench/chat/`、`logs/`、`snapshots/`、`trash/`、`exports/`、`images/` |
| **提案（Proposal）** | AI 写入类产出的唯一入口，须人工应用 | `services/proposal_service.py` |
| **门禁（Gate）** | 零 LLM 的确定性校验函数 | `backend/gates/`（章节硬门禁 `density_gate.py` / `language_gate.py` / `prose_gate.py` / 自写的 `novel_format.py`；追踪/结构校验 `tracking_gate.py` / `outline_gate.py` / `setting_gate.py`） |
| **章节元数据** | 章节标题 / 状态 / 合同ID / 字数的权威来源（正文文件只存纯正文） | `chapters` 表、`services/chapter_service.py` |
| **合同（Contract）** | 章节的验收标准（情节点/字数预算/钩子类型） | `services/contract_service.py`、`.meta/contracts.json` |
| **变更日志（Journal）** | 版本化文件变更与撤回记录 | `services/file_change_service.py`、`workspace_changes` 表 |
| **引擎** | 生成后端抽象（一次任务或持续会话） | `engine/dsh_engine.py`、`engine/direct_api.py`、`engine/dsh_chat.py` |
| **Projection（投影）** | 工作台供应商配置单向写入独立 DSH_HOME | `provider_service.project_to_dsh_home()` |

### 0.4 优化前后关键差异（基线 `b7d6fd8` → 当前工作区）

「优化前」指最后一次提交 `b7d6fd8`（纳入工作台源码的初始快照，M0–M3 主体 + 初版对话链路）；「优化后」指当前工作区（相对基线 127 个文件、+18464/−6320 行，另含大量新增文件，由多轮 AI 工具改造完成，逐轮验收见 `docs/` 各验收记录）。关键差异：

| 领域 | 优化前（基线） | 优化后（当前） | 关键落点 |
|---|---|---|---|
| 知识库与图谱 | 无独立知识体系；旧共用 `embeddings` 表 | 每书独立向量库 + BGE 本地 ONNX 语义检索 + 有据图谱（`grounded-graph-v3.3-reviewed-lifecycle`）+ AI 候选审核→提案闭环；独立 `api/knowledge.py`（13 端点）与 `Knowledge.tsx` 页面 | `services/knowledge_*.py`、`embedding_service.py`、`api/knowledge.py`、§3.10 |
| 门禁体系 | 硬门禁 3 个（density/language/prose） | 章节硬门禁 **4 个**（新增 `novel_format` 格式门）+ 追踪/结构校验门 3 个（`tracking/outline/setting_gate`）+ 独立表达诊断 `prose_quality`；质检矩阵八列 | `gates/`、§2.6、§3.4 |
| 对话权限与写域 | 仅批准强度三档 + `discussion_only` | 增加第二维度**写域**（`scope_guard`，判定顺序 deny → 写域 → 档位）；越界批准卡（`once`/`material`）；`scope_strictness`/`scope_limit_full` 偏好；持久执行契约与交付收据（`chat_execution_service`） | §3.5、§7.6 |
| 意图路由 | 硬编码 `INTENTS` 表 | **两段式路由**：注册表关键词快路径 → LLM 语义判定 → 确定性回退；多步协作计划按步串行接力 | `intent_service.py`、§3.13 |
| 大纲规划 | 候选一次性生成、覆盖旧值，冻结后无法解冻 | 候选**累计追加** + 归一化去重 + 0.92 近似过滤 + **候选对话优化**（版本链）+ 稳定 ID + **冻结/取消冻结** | `outline_service.py`、§7.2 |
| 章节状态联动 | 状态仅界面标签；基础索引依赖向量开关 | 完成/发表资格贯穿检索、前章末、摘要、发布导出；基础词法知识/图谱自动维护（不依赖向量开关）；摄取凭据与 `projection_hash` 版本失效 | `ingestion_commit/records`、`prose_history_service`、§3.10、§3.9 |
| 审稿与表达 | 审稿即结论 | 候选审稿（`review_text`，凭据绑定版本）+ 表达建议（`prose_review` 四类，独立呈现不阻断）+ 软审证据/版本保护 + 落盘前自动去味（`auto_deslop`） | §3.4、§7.4 |
| 前端架构 | 分散页面 + `window.confirm/alert` | 对话主入口全站工作区化（`ProjectChatWorkspace`/`useChatController`/`WorkspacePage`）；新增 Chat / Knowledge / BookSettings 页面；CodeMirror 6 编辑器、`Drawer`/`InlineEdit`/`CredentialInput`/`clipboard.ts`、覆盖层栈；原生对话框清零（守卫脚本 `check_no_native_dialogs.py`） | §4 |
| 引擎层 | 一次性 headless + direct-api 降级 | 新增 `dsh_headless_stdin.mjs` stdin 桥接（正文不经 argv）、对话专用 profile `ai-novel-chat`、超时/取消/重试预算管理 | §2.5 |
| 编排资产 | 6 个 Agent、7 个技能、无评测集 | **8 个 Agent**（新增 `setting-keeper`、`teardown-analyst`）+ **8 个技能**（新增 `novel-teardown`，全部含 `references/` 明细）+ `evals/` 评测目录（agent-scope / knowledge / prose-quality / chat-workspace） | 附录 A、`evals/README.md` |
| 服务层规模 | 49 模块 | **63 模块**（新增知识体系 6 个、摄取 2 个、写域/执行/意图 3 个、写作偏好、材料解析等） | §3 |
| 测试 | 后端数百项、前端 20 项 | 后端专项快照 1002–1068 passed（2026-10-04）、前端 **124 passed** + 二十余个浏览器验收脚本（不接入 CI） | §8.2 |

---

## 1. 系统总览

### 1.1 定位与技术栈

本地优先的中文小说创作工具。**Markdown 是唯一事实源，SQLite 只做可重建索引**（**例外**：章节正文为 `章节/第NNNN章.txt` 纯文本，标题/状态/合同ID/字数以 `chapters` 表为事实源，见 §3.1）。

| 层 | 技术 | 位置 |
|---|---|---|
| 前端 | React 18.3 + Vite 5 + TypeScript 5.6（无 UI 库、无状态管理库，仅 `react-router-dom`） | `workbench/frontend/` |
| 后端 | Python ≥ 3.11 + FastAPI + uvicorn + SQLite（标准库 `sqlite3`） | `workbench/backend/` |
| 生成引擎 | vendor 内置 `@deepseek-ai/dsh@0.1.0-rc.8`（子进程/原生会话） + 直连 OpenAI 兼容 API（降级） | `workbench/vendor/dsh/`、`backend/engine/` |
| 运行时隔离 | 独立 `DSH_HOME = .workbench/dsh-home` | `engine/dsh_paths.py` |
| 受管资产 | `skills/` `agents/` `rules/` `workflows/` `templates/` | 仓库根 |

### 1.2 运行时形态

- 单进程 FastAPI 服务，默认 `127.0.0.1:8790`；同时把 `workbench/frontend/dist` 作为根路径静态站点托管（SPA 回退 `index.html`，但 `api/` 前缀不参与回退）。
- 前端开发态跑 Vite dev server（`127.0.0.1:5173`，`strictPort`），`/api` 反向代理到 8790。
- **端口红线**：`config.DEFAULT_PORT = 8790`；`FORBIDDEN_PORTS = {3080, 8765, 3456}`；启动前 `check_port_available()` 探测占用，被占用即报错退出，**绝不静默顺延**。
- **已知偏差**：`config.resolve_port()` 仍保留 `WORKBENCH_PORT` 环境变量覆盖入口，与「端口固定 8790」的产品约定不完全一致（见 §8.4）。

### 1.3 代码架构图

```text
┌────────────────────────── 浏览器 SPA（React 18 + Vite + TS）──────────────────────────┐
│  AppShell（导航壳）+ ProjectChatWorkspace（项目父路由共享助手）                        │
│  pages/  Bookshelf · Chat · Editor · Outline · Bible · Cards · Knowledge · Teardown · │
│          Review · BookSettings · Inbox · Dashboard · Workflows · ImageStudio ·        │
│          Settings · NotFound                                                          │
│  components/ ChatPanel · DocumentTree · DiffView · MarkdownView · ChapterEditor ·     │
│              KnowledgeGraph · Drawer · Modal …                                        │
│  state/ useSettings · useToast · usePanelWidth · useAppearance · useChatController …  │
│  api/ client.ts（request / readSSE / ApiError） ── HTTP + SSE ──┐                    │
└─────────────────────────────────────────────────────────────────┼────────────────────┘
                                                                  │
┌────────────────────── FastAPI 后端（127.0.0.1:8790）─────────────▼────────────────────┐
│ ① API 层        api/{projects, library, models, writing, content, orchestration,     │
│                 images, knowledge}.py   —— 只做参数校验 + 调 service + 组装响应       │
│ ② 服务层        services/（63 模块）—— 业务规则、事实源落盘、索引维护                 │
│ ③ 引擎层        engine/ —— 任务执行抽象（一次任务 / 持续会话）与选择降级              │
│ ④ 门禁层（纯函数） backend/gates/ —— 章节硬门禁 ×4 / 追踪与结构校验 ×3 / 表达诊断 ×1  │
│ ⑤ 数据层        db.py（SQLite 索引） + config.py（路径与端口唯一事实源）              │
└──────────────────────────────────┬───────────────────────────────────────────────────┘
                                   │ 全部经 engine/dsh_paths.py 解析并注入 DSH_HOME
┌──────────────────────────────────▼───────────────────────────────────────────────────┐
│ vendor 内置 dsh（子进程 headless / 原生 Agent 会话）   │  direct-api（OpenAI 兼容降级） │
│   workbench/vendor/dsh/  ·  DSH_HOME = .workbench/dsh-home                            │
└──────────────────────────────────────────────────────┴────────────────────────────────┘

磁盘事实源（Markdown / JSON / 纯文本）  ←→  可重建索引（SQLite / 向量库 / 运行态）
projects/{书名}/**/*.{md,txt}               .workbench/workbench.db（36 个初始化表）
projects/{书名}/章节/第NNNN章.txt            .workbench/vector/books/<knowledge_key>/vectors.db
.meta/{contracts,outline,chat-memory,…}     .workbench/{chat,logs,snapshots,trash,exports,images}
skills/ agents/ rules/ workflows/ templates/  （章节元数据：chapters 表，正文文件只存纯正文）
```

### 1.4 分层与依赖方向（硬约束）

依赖**只能自上而下**：`api → services → engine → dsh/direct-api`；`gates` 是被 `services` 调用的叶子纯函数层；`config`/`db` 可被任意层引用。

| 层 | 目录 | 允许做 | 禁止做 |
|---|---|---|---|
| API | `backend/api/` | 路由声明、Pydantic/参数校验、调用一个或多个 service、返回 | 直接读写文件、拼 SQL、拼 dsh 命令 |
| Service | `backend/services/` | 业务规则、事实源落盘、索引维护、调用 engine 与 gates | 直接起子进程、直接调 `dsh`、绕过 `dsh_paths` |
| Engine | `backend/engine/` | 引擎进程/请求编排、协议解析、错误归一化 | 直接读写书稿（产物交回 service 处理） |
| Gate | `backend/gates/` | 对文本做确定性判定并返回结构化结果 | 产生副作用、落盘、调用模型 |
| Data | `db.py` / `config.py` | 建表、迁移、连接；路径与端口常量 | 承载业务规则 |

**唯一 dsh 入口**：业务代码禁止出现裸 `dsh` 命令或 PATH 依赖；一律经 `engine/dsh_paths.py`。静态扫描器 `workbench/cli/check_no_bare_dsh.py` 在开发期使用，`app.py:run_startup_selfcheck()` 在启动时复用它做告警（不阻断启动）。

### 1.5 目录地图

```text
d:\desktop\AiWriteStudio3\
├─ AGENTS.md                  会话入口：隔离红线 + 意图路由表 + 门禁摘要
├─ README.md                  仓库入口
├─ projects/                  小说与参考目录（第三方参考目录只读）
├─ workbench/
│  ├─ backend/                FastAPI 后端
│  │  ├─ app.py               应用工厂 + CLI 入口 + SPA 托管 + health
│  │  ├─ config.py            路径 / 端口 / 生成参数常量（唯一事实源）
│  │  ├─ db.py                SQLite 建表、迁移、FTS
│  │  ├─ api/                 8 个业务路由模块（含 knowledge.py）
│  │  ├─ engine/              9 个模块（7 个 Python + dsh_chat_runner.mjs / dsh_headless_stdin.mjs）
│  │  ├─ gates/               7 个确定性门禁 + 1 个独立表达诊断（不参与硬门禁）
│  │  ├─ services/            63 个业务模块
│  │  └─ tests/               后端测试（含 chat_acceptance_server.py、knowledge_acceptance_server.py）
│  ├─ frontend/               React SPA（src/ + tests/）
│  ├─ cli/                    setup_dsh / check_no_bare_dsh / check_ui_style /
│  │                          check_license_compliance 等运维与静态检查脚本
│  ├─ tests/                  contract / e2e / isolation / perf（部分脚本待改造，见 §8.4）
│  └─ vendor/dsh/             版本锁定的内置 dsh（不提交 node_modules 时为安装目标）
├─ skills/ agents/ rules/ workflows/ templates/   受管资产（事实源）
├─ .dsh/skills/               技能同步产物（禁止手工编辑）
├─ .workbench/                运行时（.gitignore，可删除重建）
└─ docs/                      本目录
```

---

## 2. 后端架构

### 2.1 分层与依赖方向

见 [§1.4](#14-分层与依赖方向硬约束)。一次典型请求的调用链：

```text
POST /api/projects/{id}/generate/stream        api/writing.py
  └─ generation_service.stream_task()          services/generation_service.py
       │                                       （唯一引擎调用入口：落 tasks + 计量 + 快照）
       ├─ engine/router.py: pick_engine()      resolve_route → resolve_model → 降级
       │    ├─ engine/dsh_engine.py   DshEngine（首选，subprocess + vendor dsh）
       │    └─ engine/direct_api.py   DirectApiEngine（降级，OpenAI 兼容）
       └─ engine/runtime.py           HarnessRuntime 协议 / GenerationRequest / GenerationResult
```

### 2.2 应用启动与生命周期

`app.py` 提供 `create_app()`（应用工厂）与 `main()`（CLI 入口：校验配置 → 探测端口 → 初始化运行时 → `uvicorn.run`）。

`_lifespan` 启动顺序（顺序有语义，改动需谨慎）：

1. `config.ensure_runtime_dirs()` — 幂等创建运行时目录；
2. `db.init_db()` — 建表 + 补列迁移 + 建 FTS；
3. `provider_service.project_to_dsh_home()` — 修复/重建供应商到独立 DSH_HOME 的投影（事实源仍是供应商数据库）；
4. `run_startup_selfcheck()` — 裸 `dsh` 调用静态扫描（告警，不阻断）；
5. `chat_run_service.recover_interrupted()` — 把上次进程遗留的活跃 run 标记为 `interrupted`；
6. 进程退出时 `chat_run_service.shutdown()` 收尾 live worker。

其他约定：

- 异常映射：`ServiceError` → 其自带 `status_code`（400/404/409）+ `{"detail": …}`；`FileNotFoundError` → 404。
- CORS：仅 `http://127.0.0.1:5173` 与 `http://localhost:5173`（Vite dev）。
- 路由挂载顺序：projects → library → models → writing → content → orchestration → images → knowledge，**最后**注册 SPA catch-all。
- `GET /api/health` 返回 `{status, version, port, db_ready, fts, vendor_dsh}`。

### 2.3 配置层（`config.py`）

| 类别 | 常量 | 值 | 说明 |
|---|---|---|---|
| 版本/绑定 | `VERSION` / `HOST` | `0.1.0` / `127.0.0.1` | 只绑回环 |
| 端口 | `DEFAULT_PORT` / `PORT_ENV_VAR` / `FORBIDDEN_PORTS` | 8790 / `WORKBENCH_PORT` / {3080,8765,3456} | 禁用端口直接拒绝启动 |
| 路径 | `PROJECT_ROOT` / `WORKBENCH_DIR` / `RUNTIME_DIR` / `PROJECTS_DIR` / `DB_PATH` | — | `find_project_root()` 向上找含 `workbench/` 的目录 |
| 受管资产 | `agents_dir()` `rules_dir()` `workflows_dir()` `templates_dir()` `skills_dir()` `dsh_skills_dir()` | — | **函数形式**，便于测试 monkeypatch |
| 运行时目录 | `runtime_dir()` `snapshots_dir()` `exports_dir()` `logs_dir()` `models_dir()` `vector_dir()` `images_dir()` `secrets_path()` `settings_path()` | — | 幂等创建 |
| 生成本参数 | `DEFAULT_WORD_BUDGET` | 3000 | 单章字数预算兜底值（本书区间可读时取区间中值） |
| | `MIN_CHAPTER_WORDS` | 2000 | 字数门默认下限（调用方按本书区间下限传入） |
| | `DSH_TIMEOUT_SECONDS` | 1800 | 对话活跃执行总时长 |
| | `DSH_IDLE_TIMEOUT_SECONDS` | 600 | 无模型文本/工具进展的活跃超时 |
| | `DIRECT_API_TIMEOUT_SECONDS` | 180 | 直连 API 超时 |
| | `MAX_CONCURRENT_DSH` | 2 | 同项目 dsh 并发上限 |
| | `MILESTONE_STEP_DEFAULT` | 500 | 字数里程碑阈值 |
| | `SMALL_EDIT_THRESHOLD` | 500 | 「修改模式优先」字数阈值 |
| | `CHAT_MAX_TOOL_STEPS` / `CHAT_NATIVE_MAX_TOOL_CALLS` | 6 / 40 | 对话工具步数 / 每轮结构化工具调用预算 |

工作台偏好（`.workbench/settings.json`，非事实源）由 `settings_service.DEFAULT_SETTINGS` 给出默认值；写作相关新增 `writing` 段：

| 键 | 默认值 | 说明 |
|---|---|---|
| `writing.always_inject_skills` | `["human-linguistics","novel-writing"]` | 必注入技能清单（写章节的轮次兜底注入） |
| `writing.chapter_min_words` / `chapter_max_words` | `2000` / `4000` | 每章字数区间**全局默认**（本书可用 `.meta/writing-prefs.json` 覆盖；上限硬顶 `MAX_CHAPTER_WORDS = 50000`） |
| `writing.auto_deslop` | `{enabled: true, max_rounds: 2}` | 章节落盘前自动去味重写（轮数夹紧到 `MAX_DESLOP_ROUNDS = 3`） |

对话相关偏好（同文件 `DEFAULT_SETTINGS` 的 `chat` 段，`chat_preference_service.get_scope_prefs()` 负责容错读取）：

| 键 | 默认值 | 说明 |
|---|---|---|
| `chat.permission_mode` / `chat.discussion_only` | `auto` / `false` | 新会话默认批准强度与「仅讨论」（与 `chat_preference_service.SAFE_DEFAULT` 一致）；手工改坏时回落到 `ask` + 仅讨论 |
| `chat.scope_strictness` | `ask` | 写域严格度：`ask` = 范围外需批准（默认）/ `reject` = 范围外一律拒绝（见 §3.5、§7.6） |
| `chat.scope_limit_full` | `false` | 为 `true` 时「完全访问」档也受写域限制（越界改为请求批准） |
| `chat.intent_llm` | `true` | LLM 意图判定开关：默认 `true`；显式置 `false` 时跳过 LLM 意图判定，仅用关键词快路径 |

技能正文及按需读取的引用文件没有大小预算或裁剪阈值；`services/skill_service.py` 使用 `estimate_tokens()` 展示估算用量（渐进披露见 §3.13）。真实缺失、停用和格式错误仍按原因报告。

### 2.4 数据层（`db.py`）

**核心纪律**：SQLite 是可重建索引，事实在 Markdown + JSON（**例外**：章节标题/状态/合同ID/字数以 `chapters` 表为事实源，正文文件只存纯正文）；`init_db()` 幂等（建表 + `_MIGRATIONS` 补列 + 建 FTS），`is_ready()` 要求 36 个初始化表全部存在。

36 个初始化表（`TABLE_NAMES`）：

| 分组 | 表 |
|---|---|
| 基础（M0/M1） | `projects`（含 `deleted_at` 软删除标记：NULL=在架、非 NULL=在回收站）、`chapters`（含 `title` 列，由 `_MIGRATIONS` 幂等补列）、`templates`、`snapshots`、`operation_log` |
| 生成管线（M2） | `tasks`、`proposals`、`chapter_contracts`、`prompt_registry`、`providers`、`token_usage`、`index_docs`、`embeddings` |
| 审稿（M3） | `reviews`、`quality_debts`、`style_fingerprints`、`style_fingerprints_archive`（原始样本迁移备份） |
| 编排与对话（M4/M6） | `skills`、`agents`、`rules`、`workflows`、`workflow_runs`、`chat_sessions`、`chat_messages`、`chat_runs`、`chat_run_events` |
| 角色与追踪（M7） | `characters`、`character_states`、`character_memory`、`timeline_events`、`foreshadows`、`resource_ledger` |
| 通用 | `assets` |
| 生图工坊 | `image_providers`、`image_jobs`、`image_records` |

索引与检索：

- **`workspace_changes`（惰性创建）**：文件变更日志，由 `file_change_service.ensure_schema()` 按需建表，**不在 `TABLE_NAMES` 中**，因此不参与 `is_ready()` 校验。字段含 `project_id/session_id/run_id/tool_call_id/operation/path/destination/before_state/after_state/status`，`UNIQUE(run_id, tool_call_id)` 保证同一次工具调用不重复写入。
- **`ingestion_runs`（惰性创建）**：`ingestion_commit.ensure_schema()` 创建四件套提交清单。以不可变 `book_key`、规范章节路径、正文 hash、抽取/提示版本及 AI 模式识别当前提交；记录 `plan_json/result_json/status/error`。部分唯一索引 `ingestion_one_active_chapter` 限制每书每章只有一个 preparing/applying 提交。与 `workspace_changes` 协作恢复；项目永久删除时一起清理。
- **文风唯一约束**：`style_one_chapter(project_id,rel_path)` 保证一章一条当前授权。迁移先保存被合并、路径变更或无效的原始行到 `style_fingerprints_archive`，按最新 ID 保留授权（含已取消），整个过程与索引创建共用一个事务，失败回滚，重启幂等。旧 Markdown 样本不自动认可迁移后的 TXT 正文。
- **FTS**：`docs_fts` 虚拟表（`fts5`，`tokenize='unicode61'`），字段 `project_id/rel_path/kind/body`；**不在 `TABLE_NAMES` 中**；`fts_available()` 探测失败时检索自动降级为 `LIKE`。
- **向量**：主库遗留 `embeddings` 表不再作为本书语义结果来源。新数据按不可变 `knowledge_key` 进入 `.workbench/vector/books/<knowledge_key>/vectors.db` 的 `kb_documents/kb_chunks`，模型指纹与来源 hash 随片段保存；`kb_documents.projection_hash` 单独保存可见投影版本，源章节撤回或改稿时即使追踪文件字节未变也会失效。同库保存图谱与后台任务。旧共用库只读留存，不再写入。
- **活动 run 唯一约束**：`chat_one_active_run` 索引保证每会话至多一个活跃 run（状态含 `queued/running/waiting_input/cancelling`）。

连接与事务：`get_conn()` 为 contextmanager（正常提交、异常回滚）。

### 2.5 引擎层（`backend/engine/`）

| 模块 | 职责 | 关键符号 |
|---|---|---|
| `runtime.py` | 引擎无关契约与公共实现 | `HarnessRuntime`（Protocol）、`BaseEngine`、`GenerationRequest`、`GenerationResult`、`normalize_dsh_error`、`normalize_http_error`、`ERROR_CODES`、`estimate_tokens`、`new_session_id` |
| `dsh_paths.py` | **隔离红线唯一入口**：定位 vendor dsh 与独立 DSH_HOME | `get_dsh_binary()`、`get_dsh_cli_command()`（返回 `[node, …/lib/bin.js]`，不经 shell shim）、`build_dsh_env()`、`is_vendor_dsh_installed()`、`DshNotInstalledError` |
| `dsh_engine.py` | 一次性 headless 生成（子进程） | `DshEngine`（`profile=ai-novel-workbench`）、`run_agent()`、`capabilities()`、`cancel_task()`、`list_checkpoints()`、`prune_sessions()` |
| `dsh_headless_stdin.mjs` | headless 输入桥接：读取 stdin 原始 UTF-8，重建 Node 内部 CLI 参数并加载锁定 vendor 入口 | Python 不经 `.cmd`，不把正文放进操作系统 argv；二进制管道保留 LF/CRLF，不改 vendor；超时、取消及重试仍由 `DshEngine` 管理 |
| `direct_api.py` | 直连 OpenAI 兼容 API 的降级引擎 | `DirectApiEngine`、`TRANSIENT_CODES=("RATE_LIMIT","TIMEOUT","NETWORK_ERROR","SERVER_ERROR")` |
| `router.py` | 引擎选择与三级覆盖 | `DEFAULT_ENGINE="dsh-headless"`、`FALLBACK_ENGINE="direct-api"`、`TASK_ROUTING`、`resolve_route()`、`resolve_model()`、`pick_engine()`、`engine_status()` |
| `dsh_chat.py` | **原生持续对话**运行时（每会话一个 live worker，冷 worker 从磁盘恢复） | `DshChatRuntime`、`RunControl`、`_Worker`、`get_dsh_chat_runtime()`；事件类型 `session_ready/delta/tool_call/tool_result/usage` + 恰好一个 `done/error/cancelled` |
| `dsh_chat_profile.py` | 对话专用 dsh profile 覆盖层 | `PROFILE_NAME="ai-novel-chat"`、`DISABLED_ROWS`（禁用面向模型的通用工具，仅保留只读技能加载）、`ensure_chat_profile()` |
| `dsh_chat_runner.mjs` | 对话 runner 的 Node 侧脚本 | 与 `dsh_chat.py` 配对 |

引擎选择决策（`TASK_ROUTING` 摘要）：

| 任务类型 | 引擎 | 理由 |
|---|---|---|
| 大纲构思 / 正文生成 / 审稿 / 润色 / 拆书 | `dsh-headless` | 技能包自动生效，指令注入一致 |
| 结构化抽取（摄取/分类/标签） | `direct-api` | 需要严格结构化输出，延迟低 |
| 轻量改写（< 500 字） | `direct-api` | 省 token |
| Embedding / 检索 | `direct-api` 或本地 ONNX | dsh 无该能力 |
| dsh 不可用 | `direct-api` 降级 | 核心功能不因 dsh 瘫痪 |

模型解析三级覆盖：任务级（`--patch` 按次注入）→ 项目级（`.meta/engine.json` 的 `overrides`）→ 全局默认。**注意**：`settings.yaml` 用户层优先级高于 `--patch`，故工作台不在 `settings.yaml` 固定写入 `agent-default-model`（详见 [compat-matrix.md §3](compat-matrix.md)）。

### 2.6 门禁层（`gates/`）

门禁模块统一约定 `run(text, …) -> 结果对象`：章节硬门禁返回带 `passed` 与 `report()` 的结果对象；追踪 / 结构校验门返回 `{key, passed, detail, findings, …}` 字典。按调用方分两组：

- **章节硬门禁（4 个）**，由 `review_service.run_hard_gates()` 在审稿与章节落盘时并列执行；
- **追踪 / 结构校验门（3 个）**，只报告不改写，**不参与**章节硬门禁，由对话读工具 `check_tracking` 按需调用（`账本` / `大纲` / `设定` 三类目标）；字段与格式事实源是各技能 `references/` 下的明细清单。

同目录另有原创的 **`prose_quality.py` 表达诊断**，入口 `diagnose_prose(text)`，并非新增硬门禁。返回 `{version:1, blocking:false, findings, counters, truncated}`，检测较长句段去 Unicode 空白后的精确重复，保留标点、数字、字形。段落按非空物理行且至少 80 个非空白字符 / 50 汉字，句子至少 40 个非空白字符 / 28 汉字；重复段落内的句子不重复报告。短口头禅不达阈值；有意复沓仍由作者判断。检查最多 120000 字符，最多 24 个重复组，每组最多 4 个关联位置，证据最多 320 字符，任何省略均 `truncated=true`，输入上限切断的末段和无标点尾句不参与判断。位置为原始正文的 1-based 行列、包含式末位置；重复组计数不是质量得分、AI 概率或失败数，不跨书、不查未来章，也不自动改稿。

| 模块 | 判定内容 | 关键阈值 / 常量 | 公开入口 |
|---|---|---|---|
| `density_gate.py` | 去 AI 味密度红线：破折号频次、对话引号密度、禁词表、推测词、三连排比、升华腔 | `DASH_LIMIT=250`（每 250 字 ≤1 个破折号）、`DIALOG_MIN=120`（每 120 字 ≥1 引号）、`ELLIPSIS_MIN=2`（建议值不判 FAIL）、`DEFAULT_FORBIDDEN`、`GUESS_WORDS`、`TRIPLE_PATTERNS`、`UPGRADE_PATTERNS` | `run(text, config=None, **overrides)`、`DensityConfig`、`DensityResult` |
| `language_gate.py` | 语言门：正文必须中文散文；HTML 标签/注释/实体、外语 token、路径与标记泄漏 | `MARKUP_PATTERN`、`WHITELIST_FILENAME=".deslop-whitelist"`、`CONTRACT_REQUIRED`、`CONTRACT_STALE_PATTERNS` | `run()`、`scan()`、`parse_whitelist()`、`load_whitelist()`、`check_contract_docs()`、`LanguageResult`（`to_dict()`/`to_json()`） |
| `prose_gate.py` | 中文成稿硬禁令与「模型化形状」：首句同构、句长变异系数、连词密度、借喻聚簇、短句连发、段落开头重复 | `ProseConfig`：`anaphora_min=3`、`sentence_cv_min=0.42`、`conjunction_per_1000=7.0`、`heavy_de_han=38`/`heavy_de_count=4`、`short_streak_limit=4`、`repeated_opener_min=4`、`metaphor_distance=800` 等 | `run()`、`ProseResult`、`han_count()`、`mask_non_prose()`、`dialogue_attribution_warnings()` |
| `novel_format.py`（**自写**，非第三方引入） | 正文格式门（番茄纯文本规范）：Markdown 标记泄漏（行首 `#`/`- `/`> `/围栏/`---`、行内 `**`/`` ` ``/`[]()`/`<tag>`/实体）、正文重复章节标题行、成对引号配对、半角标点混用（`...`/`--`/汉字后半角逗号句号） | `BLOCK_WARN_LINES=3`、`BLOCK_WARN_CHARS=60`、`EXCERPT_WIDTH=40`、`MAX_FINDINGS=50`、`LINE_PATTERNS`、`INLINE_PATTERNS`、`PUNCTUATION_PATTERNS`、`TITLE_LINE_PATTERN`、`PAIRED_QUOTES` | `run(text, config=None, **overrides)`、`NovelFormatConfig`、`NovelFormatResult`（`passed`/`failures`/`report()`）、`main(argv)` |
| `tracking_gate.py`（**自写**） | 资源账本算术「前值 + 变动 = 结余」逐行核对（`run_ledger`）+ 条目 ID 唯一性与格式（`run_ids`） | `ID_PREFIXES=("FACT","FORESHADOW","EVENT","FX")`、`ID_MIN_DIGITS=1`/`ID_MAX_DIGITS=6`、`LEDGER_BASE_KEY="前值"`/`LEDGER_BALANCE_KEY="结余"`、`ARITHMETIC_TOLERANCE=1e-9` | `run(text)`（= 账本 + ID）、`run_ledger(text)`、`run_ids(text)` |
| `outline_gate.py`（**自写**） | 章纲章号唯一（`error`）/连续（`warn`）/章行缺事件、卷首目标与卷末钩子成对（`warn`） | `SEVERITY_ERROR="error"`、`SEVERITY_WARN="warn"`、`CHAPTER_RE`、`VOLUME_HEADING_RE`、`HOOK_LINE_RE`、`GOAL_LINE_RE`；`passed` 仅在有 `error` 时为 `False` | `run(text)` |
| `setting_gate.py`（**自写**） | 设定 / 状态卡必填字段缺失（`error`）、来源分级缺失或取值越界（`warn`）、分类未知时列出可选分类 | `SOURCE_GRADES=("author","model","unknown")`、`CATEGORY_FILES`（8 类 → 文件）、`REQUIRED_FIELDS`（按分类的必填字段表） | `run(text, category=…)`、`category_from_path()` |

四道章节硬门禁**均为纯函数、零副作用、零模型调用**，可单独执行（各模块带 `main(argv)` 便于 CLI 自测）；三道追踪 / 结构校验门同为纯函数（无 `main(argv)`，经 `check_tracking` 调用）。阈值改动必须同步 `workbench/backend/tests/test_gates.py` 的正反例。

**说明**：`novel_format` 里的「连续多行无空行」只给**建议级**（`warnings`，不阻断）；规划记号词表的唯一事实源仍在 `review_service.MARKER_PATTERNS`（「记号泄漏」门），格式门不重抄一份，两门在 `run_hard_gates` 中并列执行。`tracking_gate`/`outline_gate`/`setting_gate` 的字段与行格式事实源是技能 `references/`（`状态维护细则.md`、`大纲结构规范.md`、`伏笔台账规范.md`、`设定字段规范.md`），**只报告不改写**。

---

## 3. 后端模块详解

服务层共 63 个模块（含公共异常模块 `errors.py`），按功能域组织。每节给出：职责 → 关键入口 → 事实源落点 → 常见改动点。

### 3.1 项目与文档树

| 模块 | 职责 |
|---|---|
| `project_service.py` | 开书：生成 `projects/{书名}/` 完整工作区（备忘录/大纲/设定/状态/章节）、项目档案、归档、删除整本（软删除进回收站 / 恢复 / 彻底删除） |
| `tree_service.py` | 文档树读取与节点新建/重命名/移动/删除；按资产分组；章节目录固定 `第NNNN章.txt`（纯正文），章节改名写 `chapters` 表并迁移元数据行 |
| `chapter_service.py` | 章节 `章节/第NNNN章.txt` 的创建/读取/保存/删除/回收站恢复；`chapters` 表元数据 upsert；存量 `.md` 迁移 `migrate_chapter_files()`；`require_chapter_path()` 等路径校验 |
| `template_service.py` | 开书模板：内置模板只读（不可写/改名/删）+ 新建空白/另存为/复制/重命名/删除；模板内文件树（新建/改名/移动/物理删除）；默认模板与「题材/平台 → 模板」偏好（落 `settings.templates`，`resolve_open_template()` 解析开书模板）；导入/导出模板包 |
| `transfer_service.py` | 章节 MD/TXT 导入导出、整书包导入导出 |
| `conflict_service.py` | 外部修改检测（mtime + hash）与行级 diff 冲突解决 |
| `fs_utils.py` | 文件系统与 Markdown 通用工具：`atomic_write_text()`（临时文件 + rename）、`split_frontmatter()`、`count_words()` |
| `writing_preference_service.py`（**新增**） | 每章字数区间：书级 `.meta/writing-prefs.json`，缺省回落 `settings.writing`；`get_writing_prefs()` / `update_writing_prefs()` / `chapter_word_range()` |

**事实源**：`projects/{书名}/**/*.md`；章节正文为 `章节/第NNNN章.txt`（**纯正文，无 frontmatter**），章节标题 / 状态 / 合同ID / 字数以 `chapters` 表为准。

**章节命名规则（当前口径）**：在 `章节/` 下新建文件时，`第7章` / `第7章.md` 这类编号会补齐为 `第0007章.txt`；输入普通文字作为**章节标题**（写 `chapters.title`），磁盘文件名取现存最大编号 + 1。删除中间章节不自动补洞；删除最大编号后新建可能复用该编号。文档树/编辑器下拉/审稿页统一用「编号 · 标题 · 字数」显示（前端 `lib/chapterName.ts`）。

**存量迁移（幂等）**：`migrate_chapter_files(project_id)` 逐个扫描 `章节/第NNNN章.md`——先 `snapshot_file(..., reason="migrate")` 存快照，再解析 frontmatter 把标题/状态/合同ID 写入 `chapters` 表，正文（去 frontmatter，仅统一换行）写入 `.txt`，最后删除旧 `.md` 并同步索引；目标 `.txt` 已存在则跳过，单文件失败只记入 `errors` 不阻断其它文件。返回 `{"migrated", "skipped", "errors"}`。触发点：打开项目（`GET /api/projects/{id}`）自动执行一次 + 手动 `POST /api/projects/{id}/chapters/migrate`。未迁移的历史 `.md` 仍可读写（元数据按 frontmatter 回退取值，不回写文件）。

### 3.2 章节与合同

| 模块 | 职责 |
|---|---|
| `contract_service.py` | 章节合同生成 / 修改 / 确认 / 冻结；CHECK 步的前置门控（大纲已冻结、合同就绪）；单章字数预算 `default_word_budget()` 默认取本书区间中值（读不到时回落 `DEFAULT_WORD_BUDGET`） |
| `outline_service.py` | 灵感扩展定向追问、每批 2–3 个候选累计生成与去重、指定候选连续对话优化（追加版本）、稳定 ID 锁定、冻结 / 取消冻结（冻结状态下修改需差异确认）、滚动规划（只细化近 2 卷） |
| `bible_service.py` | Story Bible：角色卡/世界观/时间线/伏笔；**来源分级** `author`/`model`/`unknown` |
| `asset_card_service.py` | 手动 AI 语义解析所有设定分类；后台任务、依据与版本校验、缓存失效、稳定对象 ID、原文定位与安全编辑；智能生成仍经提案 |
| `asset_card_extraction.py` / `asset_card_colors.py` | 完整原材料窗口及逐字依据协议；跨分类唯一自动颜色序列、三主题色值与手动/关闭状态 |
| `character_service.py` | 角色深度设定、字段级状态机、视角记忆、成长弧线 |

**事实源**：`.meta/contracts.json`、`.meta/outline.json`、`.meta/asset_cards.json`、`设定/*.md`、`状态/*.md`。

### 3.3 生成管线与上下文

| 模块 | 职责 |
|---|---|
| `generation_service.py` | **唯一引擎调用入口**：落 `tasks` 记录、调用引擎、用量计量、Prompt 版本与上下文快照可追溯 |
| `pipeline_service.py` | 章节生产 8 步循环与 checkpoint（精确步恢复） |
| `context_service.py` | 上下文包 9 级优先级组装 + Token 预算裁剪 + 降级事件；第 9 级写法样稿按写作入口显式开启 |
| `prompt_registry_service.py` | 提示词注册表（`PROMPTS` 为事实源） |
| `token_service.py` | Token 计量与成本预估；近 N 天含今日，起点为 `today-(N-1)`，截止今日（含首尾，排除未来日期），返回 `period.start/end`；历史成本汇总 `token_usage.cost_estimate`，不按当前单价重新计费 |
| `proposal_service.py` | **提案收件箱**：AI 写入类产出的唯一入口，应用前强制 diff |
| `agent_loop.py` / `chat_tools.py` / `chat_workspace_tools.py` | 对话工具表与工具桥（读工具无副作用；写工具统一走变更日志/提案） |
| `operation_log.py` | 操作日志（删除去向、索引重建等） |

**上下文 9 级优先级**（`context_service.LEVEL_TITLES`，超预算自低向高裁剪）：

```
1 用户显式材料            5 角色当前状态（必读，MANDATORY_LEVELS=(3,5)）
2 本章细纲与合同          6 伏笔台账
3 上一章结尾 500 字（必读） 7 历史章节摘要（滚动窗口）
4 设定 + 同题材拆书事实卡   8 检索结果（向量/FTS）
9 本书认可写法样稿（可选，先于事实材料裁剪）
```

常量：`DEFAULT_INPUT_BUDGET=32000`（tokens）、`PREV_TAIL_CHARS=500`、`MANDATORY_LEVELS=(3,5)`。必读集即使在超预算时也保留，并记录降级事件。

`assemble(include_style=False, style_query=None)` 保持既有调用默认行为；写作管线、可写章节对话步骤、写作助手与局部语言操作显式开启样稿。合同上下文同时序列化 `plot_points` 与 `must_connect`。样稿以独立 `type="style"` block 注入，不能替代当前章事实或授予工具权限。`style_reference` 区分 `selected_samples/selected_reference_hash/selected_tokens` 与实际 `samples/reference_hash/tokens/injected`，预算裁掉后实际字段清空；预览、上下文审计和生成任务快照记录实际注入结果。

管线 DRAFT 明确把 writer 的注册技能正文、本书规则摘要和版本化提示词传给统一生成服务，Direct API 降级也接收这些输入。草稿、提案及合同分别绑定摘要；管线提案只能应用逐项通过且摘要仍匹配的候选，编辑提案后旧审稿凭据失效。合同文件提交与提案应用共用按书锁，避免校验后、正文提交前合同在工作台内被更改。

第二轮表达优化的事实源为 `prompt_registry_service.PROSE_CRAFT_GUIDANCE`，本项目原创共享指导进入章节生成（v4）、续写（v3）、精确修订（v2）、软审（v2）和对话自动去味的实际 system 输入。指导按当前目的、关系与知识边界区分对白，保留必要说明、线索、心理与长句；动作/对白已经传达的含义不再逐句解释，不为了凑字数、对白占比或句长新增事实、方言或口头禅。合同审稿提示升级 v3，以同一次模型调用可选返回有连续原文证据的表达建议，不增加默认调用轮次。

生成前记录目标正文 hash，生成后提交候选前在同一按书锁内重查正文与合同，作者期间改稿时不自动改用新基准。修订也核对旧提案 `base_hash`，不能把旧候选重新基于新稿提交。提案创建成功后才替换草稿当前版本，失败保留旧稿。提案编辑、丢弃与应用共用按书锁，应用锁内重读状态和 payload，取消守卫传到实际文件提交前。

作者在收件箱编辑管线候选后，可调用 `review_pipeline_candidate()`（HTTP `/proposals/{id}/review`）审当前候选全文并重新绑定凭据，然后明确应用；审查本身不写正文。审稿期间候选、基准正文或合同变化时拒绝绑定通过凭据。此入口独立于旧管线检查点，不要求把未审核候选先写入正文。

### 3.4 审稿与门禁

| 模块 | 职责 |
|---|---|
| `review_service.py` | 硬门禁调度 + 逐项合同验收（**待核实不算通过**）+ 三层一致性 + 质量债；`review_text()` 审传入候选，`review_chapter()` 读盘后复用；`soft_deslop()` 产出行级 patch；`quality_matrix()` 校验当前正文与合同版本 |
| `style_service.py` | 文风指纹与写法参照：采样绑定正文 hash；`generation_reference()` 从本书有效认可样稿选最多 3 个片段，完整输入不超过 1600 tokens |
| `ingestion_service.py` | 章节摄取总控：四件套（时间线/资源账本/伏笔埋设走确定性托管写入，角色状态变更一律生成 Proposal）+ 滚动摘要（`.meta/summaries.json`，供上下文第 7 级）；按正文版本幂等提交，AI 结构化抽取可选、失败明确降级；流程详见 §7.5 |
| `ingestion_records.py` | 原文证据定位、有限数值与结构校验、AI/规则事件去重；替换本章系统托管块并保留人工材料和已确认伏笔 |
| `ingestion_commit.py` | 每书每章提交声明、重放、版本校验、中断恢复/回退；专用内部摘要写入及章节路径引用迁移 |
| `prose_history_service.py` | 此前完成/发表章的只读、版本绑定正文引用，供重复诊断与写作参照避免复写；不回填元数据、不读取任意整书（`require_chapter_path` + `managed_path` 双重边界） |
| `material_parser.py` | 设定/状态/时间线/伏笔共享确定性解析：标题、对象表、属性表、列表的实体归属、真实来源与原材料位置 |
| `rule_service.py` | 规则体系：全局（`rules/`）+ 作品级（`.meta/rules.json`）+ 技能内置；优先级链解析与编译（产物 `.dsh/skills/workbench-rules`） |
| `routing_service.py` | 意图路由**门面**（`classify()`/`route()`/`describe()` 语义兼容）：内部委托 `intent_service`，触发词与意图表来自 Agent 能力注册表（不再硬编码 `INTENTS`）；详见 §3.13 |

**硬门禁清单（`review_service.run_hard_gates()`）**：矩阵按 `key` 映射，不依赖数组下标。

| # | 门 | 判定 | 阻断 |
|---|---|---|---|
| 1 | 字数门 | 正文汉字数 ≥ `min_words`（调用方按本书区间下限传入） | 是 |
| 2 | 语言门 | `language_gate.run()`（中文散文；白名单只在本书内查） | 是 |
| 3 | 禁词门 | `density_gate.run()`（禁词 / 推测词 / 三连排比 / 升华腔 / 破折号 / 对话密度） | 是 |
| 4 | 去AI味门 | `prose_gate.run()`（模型化形状） | 是 |
| 5 | 记号泄漏 | `MARKER_PATTERNS` 规划记号 / 任务指令泄漏 | 是 |
| 6 | 格式门 | `novel_format.run()`（Markdown 标记泄漏 / 重复标题行 / 引号配对 / 半角标点） | 是 |
| 7 | 字数上限 | 正文汉字数 ≤ `max_words`（本书区间上限；`max_words=None` 时不产出该项） | **否**（仅告警） |

`min_words` / `max_words` 由调用方传入：`review_chapter()`、`quality_matrix()`、`file_change_service._prepare_change()`（章节落盘）与对话工具 `run_gates` 均取 `writing_preference_service.chapter_word_range(project_id)`。

**事实源**：`.meta/reviews/…`（审稿记录）、`设定/…`、SQLite `reviews`/`quality_debts`/`style_fingerprints`。

**质量债分级**：债-1 章级瑕疵（登记放行）→ 债-2 一致性问题（放行并标记待校订）→ 阻断（停链，回合同/大纲层）。

候选审稿 `review_text(project_id, chapter_rel, text, *, use_ai=True, register_debt=False, candidate_hash="", should_cancel=None)` 不要求正文先应用到目标文件。摘要按原始文本 UTF-8 SHA256 计算，传入摘要不一致在模型调用前拒绝。审稿将合同快照明确交给模型，并确定性补齐 `plot_points/must_connect` 遗漏项；“已完成”证据必须是候选正文的连续原文，返回证据行列，伪引用或缺引用降为待核实。AI 失败、结构错误、无结果及审稿期间合同变化均不能通过。记录保存 `candidate_hash/contract_hash/contract_changed/ai_error`；质检矩阵不把未应用候选或旧合同审稿显示为当前稿通过。

同一审稿结果新增 `prose_quality` 和 `prose_review`。前者是确定性重复诊断；后者仅收「重复解释 / 人物同声 / 模板表达 / 机械转场」四类模型意见，最多处理 12 条，要求类型合法、问题/建议非空、长度有上限，连续证据在正文唯一且至少 4 字符，行列由服务器定位。无效或歧义意见丢弃并记录 `rejected_expression_suggestions` 数量，不影响合同验收。两者不参与硬门禁、合同 counts、一致性、自动重试或质量债；模型整体结论与逐项矛盾时仍按既有保守规则转待核实。仅因合同/硬门禁未过已经进行精确修订时，将至多 4 条表达意见作为单独的可选参考，不能挤占必改指令或扩大补丁范围。

独立 `soft_deslop()` 仍审当前磁盘正文，使用书内 review 上下文和显式认可样稿，并记录实际 `style_reference` 与原始全文 `candidate_hash`。建议数组成功（包括空数组）才 `ai_used=true`，模型/解析/上下文失败返回 `ai_error`；单条坏建议记录 `rejected_suggestions` 而不抛异常。行号仅接受真正整数或纯数字字符串，拒绝 bool/浮点/越界；原文必须在指定正文行精确出现且在原始全文唯一，原文与替换不可跨行、为空或相同，重复/重叠建议只保留首条。最多处理 50 条。模型调用后按书锁重新解析安全源路径并校验全文 hash，源改变、删除或路径失效则 `source_changed=true`，保留意见供查看但不创建过期提案；正常提案基准与受审全文一致，仍由作者应用。

样稿认可绑定采样时的正文版本，`sample_version=1/content_hash/note` 位于 `style_fingerprints.payload`。采样按书锁 upsert 当前章记录，同版重复不会增加统计权重；未传 note 保留原备注，显式空字符串清空。项目限定取消认可立即退出统计和写作参照。列表、统计和生成使用同一有效性校验，取消先取 100 条的截断。未认可、迁移旧 MD、旧无 hash、源稿改变、删除或不安全路径均排除，并返回 `usable/reference_status/reference_reason`；作者需重新采样并认可。指定目标章时只选此前章节，排除目标及未来章节；按场景、角色与作者备注确定性匹配。统计只作软参照，片段只学叙述与对话写法，不能继承情节。无有效样稿时明确降级，正常写作继续。

审稿/软审返回 `source_hash/task_id/ai_error_code/ai_error`，错误信息经脱敏与截断，区别引擎失败、结构错误、建议原文核验失败和成功空建议。审稿页将逐项审稿、去 AI 味软审和提取四件套作为三个显示模式；切换只显示对应结果，独立开始按钮才执行。切模式保留当前任务，切章/切书使旧响应失去提交资格。正文 hash 变化标记旧报告，旧依据不再直接定位。质量矩阵显示字数门、语言门、禁词门、去 AI 味、格式门、硬门禁总状态、一致性、逐项审稿八列。总字符数与正文汉字数分别标示。

`review_text()` 的近期正文诊断使用当前候选正文，`_ai_review()` 注入相同的来源、参照 hash 和诊断摘要；作为表达参考，不进入硬门禁或质量债。近期正文读取先将 CRLF/CR 统一为 LF，与 `fs_utils.read_text()` 的正文版本规则一致，避免 Windows 下参照快照 hash 误报过期。

章节树状态菜单复用 PATCH chapters，只更新目标章节元数据；与当前编辑文件无关，保留未保存正文及原编辑基准。`useScopedAction` 在同步事件与 render 期维护操作 token，防止双提交和切书后旧 finally 解除新任务忙碌；用于审稿、设定、卡片、收件箱、大纲、拆书等写操作。大纲保存/扩展使用提交快照，保留请求期间的新输入；后端候选锁定在书锁内重读保存后的历史再更新锁定信息。

### 3.5 对话运行、权限与恢复

| 模块 | 职责 |
|---|---|
| `chat_service.py` | 工作台会话与消息管理、Agent 选择、标题任务调度、快捷卡；`get_session_permission(session_id)` 供执行期实时读取权限；**写域构造的唯一入口** `material_dirs()` / `round_scope()` / `scope_label()` |
| `chat_run_service.py` | **运行生命周期**：DSH 持有模型/工具迭代；本服务持有项目权限、请求幂等（`client_request_id`）、取消、可回放事件日志、恢复与收尾；`_prepare()` 消费路由结果生成本轮 Agent / 技能 / 工具白名单（按 Agent `tools` 过滤）/ 写域；多步协作计划时**按步串行接力**（每步独立 system / context / scope / 工具清单）；每次工具调用前刷新权限、必注入技能兜底、回复质量体检 |
| `chat_interaction_service.py` | 运行中工具的人工决策（批准/提问）持久化，JSON 为事实源；`has_task_approval(run_id)` 支持任务级批准（越界授权**不**纳入其免询范围） |
| `chat_preference_service.py` | 该本书新会话的默认权限（`SAFE_DEFAULT = {permission_mode: "auto", discussion_only: False}`）；全局「写域」偏好 `get_scope_prefs()`（严格度 + 完全访问是否受限） |
| `scope_guard.py`（**新增**） | 写域守卫（**纯判定 + 留痕，不落盘、不加表**）：`build_scope` / `evaluate` / `grant` / `grants` / `describe` / `trace`；详见 §7.6 |

**状态机**：`ACTIVE = (queued, running, waiting_input, cancelling)`；`TERMINAL = (completed, failed, cancelled, interrupted)`。同一会话同时至多一个活跃 run（数据库唯一索引保证）。

**权限模型（两个正交维度）**：维度一仍是批准强度——每会话持有 `permission_mode ∈ {ask, auto, full}` + 独立 `discussion_only`，会话本身是权限事实源；`chat_run_service._execute()` 在**每次工具调用前**调 `chat_service.get_session_permission(session_id)` 重读库并刷新 `tool_context` 的 `permission_mode` / `read_only`——运行中改权限后当前任务的下一个工具调用即按新值判定，不再需要新开对话。轮次展示仍可用指挥时的快照（`request["session"]`）。

维度二是**写域**（`write_targets`，本轮可写材料集合）：由路由产出、经 `chat_service.round_scope()` → `scope_guard.build_scope()` 归一为 `{dirs, files, declared, label}`，写入 `tool_context["scope"]`。判定顺序固定为 **只读 / 仅讨论拒绝（deny 优先）→ 写域 → 批准强度**，由 `chat_workspace_tools._write()`（兼容路径 `chat_tools.py` 同语义）在每次写调用时执行。所有执行轮次均建立写域，空写域不授予任意文件权限。`agent_service.READ_ONLY_AGENTS` 固定约束 `reviewer`、`teardown-analyst`、`chief-editor`、`writing-assistant` 四类只读角色；工具白名单与运行时共同拒绝写入和撤回。

| 模式 | 书稿变更行为（范围内 / 范围外） |
|---|---|
| `ask` 请求批准 | 所有变更先展示路径、操作与归一化实际差异，作者批准后继续；批准卡提供「本任务内不再询问」（`{"decision":"approve","scope":"task"}`，仅本 run 内有效）。范围外写入同样弹卡（带越界说明），且**不受**「本任务内不再询问」豁免 |
| `auto` 帮我批准（默认） | **范围内**：新建、改写、整章替换自动执行；仅删除、移动（破坏性操作）请求批准。**范围外**：不走自动放行，降级为与删除/移动同形态的批准卡（`payload.scope` 带越界材料、理由与「仅此次（`once`）/ 本任务内允许这类材料（`material`）」两个选项） |
| `full` 完全访问 | **范围内**：本书内受支持的文档操作自动执行（含可撤回的删除/移动/覆盖）。**范围外**：默认放行但**强制留痕**（运行事件 + 操作日志 + 结果 `scope.resolved="full_allow"`）；`chat.scope_limit_full=true` 时改为请求批准 |

**写域的三条补充语义**：

- **自由便签区**：`scope_guard.ALWAYS_WRITABLE = ("备忘录",)`——写 `备忘录/` 永不判越界（不受本轮写域限制），它既不构成「改错材料」也不会顶替本轮产出；
- **严格模式**：`chat.scope_strictness="reject"` 时范围外写入**直接拒绝**（不弹卡），返回 `code="out_of_scope"` 并把拒绝原因回灌模型；只读 / 仅讨论状态下任何写入一律被拒，不受档位与写域影响；
- **越界授权**：作者选「本任务内允许这类材料」→ `scope_guard.grant(run_id, 材料)`，**仅存进程内**、run 结束即失效，与既有「本任务内不再询问」是**两个独立开关**（互不扩权）。

三档**共同**执行：先读后改（`expected_hash` 版本比对）、章节硬门禁、快照与撤回、路径边界（隐藏路径/跨书/链接绕过一律拒绝）。`discussion_only` 只允许保存对话与本书记忆，且**一旦开启，本任务内写操作立即一律拒绝**（不因权限放宽而放行）。默认值：`chat_preference_service.SAFE_DEFAULT` 与 `settings.chat` 均为 `auto` / `discussion_only=False`；写域严格度默认 `ask`、完全访问默认不受写域限制。

**计时**：整个 run 共用 `RunControl`，`DSH_TIMEOUT_SECONDS=1800` 为所有模型步骤累计的活跃执行预算，`DSH_IDLE_TIMEOUT_SECONDS=600` 为连续无模型文本或工具进展的上限；人工回答/批准等待仅扣除一次。工具总预算为 40 次，超限结束任务而非持续把错误回灌模型。

工具执行由受控后台线程承载，事件循环每 50ms 核对共享预算及取消；停止信号锁存，丢弃迟到结果。文件提交、撤回和自动去味继承取消守卫，自动去味只使用剩余活跃预算。Python 无法强制终止已阻塞的底层 OS I/O；任务能终结、迟到提交受守卫约束，不能据此宣称所有底层调用立即退出。明确的供应商额度/credit 错误映射为 `QUOTA`，普通 401/403 仍为 `AUTH`、429 仍为 `RATE_LIMIT`。

章节自动去味遵守原操作范围：`replace_exact` 仅把替换片段交给模型，片段前后各最多 1200 字作为只读依据；仍以原 `old_text/expected_hash` 提交精确替换，每轮检查合成后的整章，不升级为整章重写。范围外问题、无有效改动、模型失败或取消维持原门禁拒绝。只有作者原本请求的 create/rewrite 才处理完整正文。任务快照保存 `rewrite_scope/input_hash/style_reference`，生成提示要求保留事件、数字、视角与人物信息差。

自动去味改变实际参数时，`file_change_service.apply_change(source_request=...)` 将原工具、完整原参数及初始解析请求绑定到既有 journal `request_json`，无新增数据库列；`request_hash` 继续只摘要实际有效写入参数。重放先核验当前权限与完整来源，来源一致才复用有效参数，并再次核验有效摘要，不重复模型、写盘或 journal；参数变化拒绝。旧无来源记录不推测修订映射，仍按原有效摘要核验。已撤回记录重放不恢复文件、不标为已应用，也不更新当前读取 hash。审批预览仍只展示实际有效修改。

**事实源**：`.workbench/chat/sessions/`（会话与消息快照）、`.workbench/chat/runs/<run_id>/`（请求、事件、交互）；SQLite 为可恢复索引。

**2026-10-03 持久执行契约**：`chat_execution_service.py` 集中提供模型材料预算、计划指纹和交付收据核验。`_prepare()` 将当前文件、正文目标及本会话最多 4000 字提示传给路由；材料参考不扩大写域，否定材料从写域剔除。所选模型窗口扣除完整 system/技能、工具定义、最大输出、20% 历史与安全余量后再计算本轮材料预算，材料上限仍为 32000 tokens。历史压缩只由锁定的原生 dsh 管理；压缩活动仅传安全元数据，不持久化摘要或原始模型输出。

`intent_service.report_only()` 将“只报告/只提修改建议”等作者约束同时用于路由结果和运行守卫；审稿语义依据注册表中只读角色的能力触发词优先判定，正文/世界设定等依据词不会将审稿改路由为写设定。全局只读请求清空写域与计划。明确“先审稿，只报告问题；再由 writer 改写正文”的首步保持只读，后步按作者授权给 writer 写域；首个只读角色不能吞掉后续写步骤。“全程/整个任务/本轮只报告”、会话仅讨论及“不要修改任何文件”仍禁止整轮写入。LLM 路由未返回计划时，明确“先…再/然后/接着…”的多个产出仍保留来自注册表的确定性串行计划；参考材料不成为产出步骤。

受理先建立数据库请求身份，再保存请求/运行/会话及事件，落盘故障终结对应身份并释放会话占用；相同 `client_request_id` 返回原任务，执行情况不明时不自动重放。多步任务保存稳定步骤、指纹、状态与实际 journal 收据，写步骤没有已核验落盘凭据时返回 `DELIVERY_UNVERIFIED`，依赖步骤不继续。明确续接逐项重验原收据和当前文件 hash，只复用仍有效的完成步骤。

`delivery_targets` 与 `write_targets` 分开：前者描述作者实际要求的具体文件或材料，后者约束角色获准修改的范围。例如 context-keeper 可以修改 `状态/` 与 `设定/`，作者只要求 `状态/资源.md` 时仅核验该文件；写入其它获准文件不能顶替目标。具体路径、同目录两份文件、当前选区和正文目标逐项核验，参考及否定材料剔除。收据按 journal ID 的实际顺序核对最后版本，复用旧收据不会覆盖当前新版本。

只读完成步骤保存 `report={text,text_hash,input_versions,context_fingerprint,system_fingerprint,reusable}`。续接重新组装当前注入材料，核对报告文本、实际读取文件 hash 与系统规则；均一致才复用并向下一步交接报告。旧记录缺凭据、材料/报告被改、无版本依赖或使用未能版本化的查询工具时重新核验。`plan_state.steps` 与路由元数据中的 `delivery_targets/report` 均为可选字段，旧会话仍可读。

`POST /chat/runs/{id}/revert` 和模型撤回工具共用 `authorize_revert`。HTTP 路径按当前会话权限、钉住角色或原执行角色、原任务写域和作者禁改材料重建上下文，禁止撤回活动任务或同会话另一个活动任务。作者直接点击撤回只作为所选变更的一次明确批准，不授予后续工具写域；只读/仅讨论及严格范围拒绝仍优先。撤回前后继续核验当前文件版本并保留 journal。

受理数据库事务失败整体回滚；请求、run、会话投影、started 事件各个持久点故障均受测试覆盖。线程构造或启动失败将已受理身份终结为 `RUN_START_FAILED`，释放会话占用；重试同 ID 返回原失败身份，不能隐式重跑一次未知任务。

run 快照、助手消息 meta 及事件可带三个兼容字段：`plan_state={fingerprint,active_step,steps}`、`completion={status,receipts,error_code}`、`metrics={tool_calls,model_steps,first_delta_ms,compactions,active_ms}`。`steps` 的状态为 pending/running/verified/reported/failed；completion 区分 verified/reported/unverified/failed/cancelled。旧记录无字段时正常读取。收据只引用既有 `workspace_changes`，不增加第二份文件事实源。

### 3.6 对话记忆与交互

- `chat_memory_service.py` — 书籍级对话记忆：`projects/{书名}/.meta/chat-memory.json` **为事实源**，SQLite 仅用于校验引语真伪。只保存作者**明确确认**的事实/偏好/决定；含糊回答与 AI 建议标为待确认、不注入；支持更正/删除/来源跳转；按书隔离。
- `chat_interaction_service.py` — 批准与问答共用磁盘交互记录；回答作为原工具调用结果返回；支持刷新、重复提交、停止与重启的状态区分。`has_task_approval(run_id)` 判定本 run 内是否已选择「本任务内不再询问」：只看该 run 的批准记录（`response.scope == "task"`），不写持久表，任务结束即失效，不影响其它会话或后续轮次。

### 3.7 文件变更、快照与版本

- `file_change_service.py` — **项目级、版本化文件变更日志（journal）**，对话与提案应用共享：
  - `prepare_change()` / `preview_change()` / `apply_change()` / `revert()` / `recover_pending()`
  - `assert_version()`（`expected_hash` 校验）、`project_lock()`（项目级互斥）、`managed_path()`（路径边界）：章节为 `章节/第NNNN章.txt`（未迁移的 `.md` 仍可读写），其余文档仍限 `.md`
  - `_normalize()` 对 `章节/` 候选只保留纯正文（不写 frontmatter）；`_sync_chapter_meta()` 在落盘后把标题/状态写回 `chapters` 表
  - journal 自持 before-image，普通快照清理**不会**删除撤回数据；`applying` 行可见化中断提交
  - 门禁拒绝抛 `GateRejectedError`；版本过期抛 `FileConflictError`
- `snapshot_service.py` — 版本快照：保存/应用前存档到 `.workbench/snapshots/`，支持恢复。

### 3.8 拆书资产库

- `teardown_service.py` — 导入目标 → **自定六维**拆解（`DIMENSIONS`：开篇钩子 / 情绪节拍 / …）+ 双时间线 + 事实卡（**强制章节依据**，无法取证标「依据缺失」且不补写）→ 按题材召回注入上下文第 4 级。
- 关键入口：`list_targets()`、`import_target()`、`analyze()`（确定性统计 `analyze_deterministic()` + AI 维度分析）、`list_facts()`、`recall_for_genre()`、`read_artifact(kind ∈ source/analysis/timeline/facts)`、`delete_target()`。
- 事实源：`projects/{书名}/拆书/{目标书名}/`；索引：`assets` 表。

### 3.9 发布导出

- `publish_service.py` — 按目标平台整理发布稿 + **提交前自检清单**。
- 平台档案 `PLATFORM_PROFILES`：`通用`（默认，`1800–4000` 字）、`起点中文网`（`2000–4000`，标题「第N章 标题」，段落缩进 2）、`晋江文学城`（`2500–6000`，缩进 2）。每个档案声明 `chapter_title` / `use_markdown_heading` / `paragraph_indent` / `chapter_separator` / `file_suffix` / `word_range` / `notes`。
- 字数区间取值：`_effective_word_range()` 优先取**本书字数区间**（`writing_preference_service.get_writing_prefs()` 的 `source == "book"` 时），否则回落平台档案的 `word_range`；导出正文直接取章节纯正文（不再剥 frontmatter）。
- 自检项：字数区间、章节状态、未解决质量债、简介与题材、无凭据泄漏。
- 发布导出的 `only_completed` 指已定稿章节，包含「完成 / 发表」及既有英文兼容值；发表章节不再被过滤或标作未定稿。导出本身不会改为发表，实际状态仍由作者设置。
- 导出落 `.workbench/exports/`。

### 3.10 向量检索与索引

| 模块 | 职责 |
|---|---|
| `knowledge_store.py` | `project_id` → 服务端不可变 `projects.knowledge_key` → 本书数据库；每次连接核验 `book_identity`；路径及链接检查、按书锁、独立偏好、仅删除本书知识目录 |
| `embedding_service.py` | 固定 BGE 中文模型制品 SHA-256 校验、后台准备及中断恢复、CPU ONNX 三输入 INT64、CLS 池化与 L2；查询与正文不同编码；按段落/句末分块（最多 384 内容 tokens、64 overlap），完整保留偏移与行号；无模型则明确报词法降级 |
| `knowledge_service.py` | 受控材料枚举、增量索引、基础图谱、来源/引用校验、实时 hash/状态失效、词法与语义 RRF、有依据一跳展开、流式问答；图谱升级独立于向量与AI候选分析 |
| `knowledge_candidate_service.py` | 按书5秒合并、带版本/原文依据的AI实体/字段/关系候选、分块缓存、独立可取消任务、冲突/歧义、候选审核与按文件提案、提交凭据回填、旧AI关系迁移 |
| `knowledge_lifecycle.py` | Markdown知识记录序列化与解析；审核/生命周期/章节范围的统一投影，保持原文偏移；图谱、检索及上下文直读共用 |
| `vector_service.py` | 旧向量接口兼容层，所有入口绑定本书知识库；`model_info`/`set_embedder_config` 支持全局继承或本书 local/cloud/lexical；旧 hashing 函数仅保留为诊断函数，不产生可用语义索引 |
| `index_service.py` | 扫盘重建 SQLite 索引（章节表 / 文档表 / FTS）；扫盘**只更新** `chapters` 的字数与版本（`hash`/`mtime`），标题/状态由 `chapter_service` 维护 |
| 前端 `knowledgeState.ts` / `knowledgeGraphState.ts` | 书籍身份和实际请求范围核验、资料/实体关联、证据定位；Cytoscape增量元素更新、按书及范围的节点/视口缓存、边分类与限幅适配 |

**物理存储与绑定**：每书UUID `knowledge_key`及`book_identity`双重核验不变。独立库保留`kb_*`索引/图谱表，并新增`km_candidates`、`km_jobs`、`km_extract_cache`、`km_reviews`、`km_meta`；普通同步只更新`kb_*`，不会清除候选及审核记录。Markdown为已应用知识事实源，候选数据库记录审核与提交凭据。候选ID和实体定义锚点独立于带原文偏移的图谱节点ID。

**向量版本与图谱版本**：向量版本仍为`story-knowledge-v1:paragraph-sentence384-overlap64-v2`；图谱版本升级为`grounded-graph-v3.3-reviewed-lifecycle`。图谱升级从有效原文重新投影，不重新生成向量。普通定义节点返回与候选服务相同的稳定`anchor`，已审核新增实体沿用Markdown中的`entity_anchor`；候选与既有实体按锚点复用，不按名字猜测合并。字段节点标签只显示实体、字段和建议值，原资料中的知识小节标题与章节范围保留在完整出处和元数据中；已审核关系按知识ID保持独立边，同关系的计划与历史、不同适用章节不会合并丢失生命周期。旧`projection=model`关系经当前文档hash、原文引句和双方实体校验后迁移为待审候选；材料本身不变，未经审核的AI关系不作为已确认剧情关系。

**模型与失败语义**：本地为 `BAAI/bge-small-zh-v1.5`，Qdrant ONNX revision `46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59`，512 维；文件清单/hash、tokenizer、池化、归一化、查询前缀和分块版本纳入指纹。权重共享于 `.workbench/models/bge-small-zh-v1.5/`，书稿、向量与任务独立。仅显式配置使用云嵌入；本地失败或指纹不匹配会返回 `degraded_reason` 与关键词证据，禁止自动转云或混入哈希向量。全局变化仅重建有效配置发生变化且已启用的书。本书同步或全局准备启用后，后续保存自动维护；`app._lifespan` 恢复未完成知识任务及已请求的模型准备。

**索引范围**：历史正文只接受 `chapters.status ∈ 完成/发表`（兼容 completed/published）；目标章明确时排除目标章与后章正文。章节摘要另由上下文第 7 级直接读取并作完成/发表、后章过滤；JSON 摘要没有能在编辑器准确选中的原文跨度，故不进入可引用证据库。设定、状态、大纲为受控目录；大纲标记计划，当前状态不能证明早期状态；拆书只在专属 profile 返回原创带章节依据的事实卡原文行，原书正文、技能、规则、待应用稿、备忘录不进剧情索引。草稿列为“未纳入历史索引”，仅本次显式选定后可查。对话工具还要求草稿在作者已授权附件/目标文件内。

**章节状态与基础知识维护（2026-10-04）**：标题/状态仍以 `chapters` 为事实源。保存、标题/状态修改、删除、移动、恢复先失效旧依据，再合并排队更新本书知识；打开已有项目也安排核对。基础词法片段和图谱不再依赖 `knowledge_enabled` 或向量开关，草稿转完成/发表后自动入库，回退草稿后退出历史。未启用语义索引时 `enqueue()` 使用 `sync(enable=False, lexical_only=True)`，不解析嵌入模型、不下载或调用云嵌入，不开启 AI 分析偏好；显式启用的书继续使用作者配置。基础同步是后台任务，页面区分待索引、已索引和失败，不把排队视为入库成功。关闭工作台前未完成的词法任务与语义任务均由 `recover()` 恢复。普通索引同步不等同于提取四件套或批准知识候选。

**追踪记录资格与证据版本**：`ingestion_records.project_text()` 通过 `ingestion_runs` 最新已提交清单核验 `wb-ingest` 区块的本书来源、章节定稿状态和正文 hash；未知、畸形或失效托管块不进入知识库、自动上下文和专用设定/状态/伏笔读工具，原作者文件保留。原文件 hash 用于编辑器定位，`projection_hash` 用于检测可见材料变化，索引和引用不得只凭原文件未变继续使用旧记录。有摄取凭据的滚动摘要必须匹配源版本和已提交摘要值；旧无凭据摘要暂兼容完成/发表过滤，缺少版本证明这一限制单独记录。

目标章已明确时，同章及后章摄取块也从历史上下文和引用中排除。AI候选分析使用同一摄取投影并保留原文件 hash；含摄取块的分析缓存增加可见投影指纹，普通章节仍按分块增量复用。待审核/待应用候选的依据必须在当前投影中仍逐字可见，源章撤回、改稿或删除后即失效，图谱待审覆盖层同步退出；作者此前已确认应用的正式知识继续保留。后台同步在检查无后续请求的同一锁区间移除运行登记，避免任务收尾窗口漏掉新状态请求；旧任务清理不能移除其后注册的新任务。

**基础材料解析**：设定卡读取一至六级 Markdown 标题，跳过 frontmatter、代码围栏中的标题、通用栏目、字段标题和空的分组标题；字段小节并入所属卡片。表格只有声明名称列时才产生条目，伏笔文件支持编号/加粗条目；势力文件中的“格局/生态/总述”按概念处理。来源先取已有来源映射，再读卡片正文 `来源` 字段；`author/model/unknown` 保留，混合或不能确认的声明归为 `unknown`，不推断作者确认。伏笔中的计划/未埋设声明进入 `planned` 并使用计划定义/计划章节边。卡片节点以类型、文件、区段偏移消歧；仅显式 `名称/姓名/正式名` 或 `别名/化名/又名` 参与原文提及匹配，同名歧义时不自动连提及边。

**图谱事实纪律**：已有设定/状态/时间线/伏笔投影基础实体、章节、文档及定义/提及边；共同出现或向量相似不生成剧情关系。AI抽取进入独立候选，不直接写作者文件。节点和边返回来源、审核、候选关联、生命周期、适用章节及精确出处；模型来源通过作者审核仍保留。正文片段默认隐藏，仅显式展开；每次最多300节点，裁剪清除孤立边。实体目录单独分页，不受画布300节点上限影响。

`v2.2` 对标题、表格与编号列表共用 `_markdown_lines()`，统一排除 frontmatter 和匹配代码围栏，围栏内示例不建立实体。伏笔计划状态仅由文件前言和当前条目决定，当前条目明确“已埋设/已回收”优先，前一条的计划不会传播到下一条。

**后台与生命周期**：保存、删除、移动、撤回/恢复及状态修改先失效索引，再独立排队维护索引及AI分析。知识页`overview()`轮询还比较本书持久化`observed_materials`路径/hash/状态快照，检测外部编辑或未索引草稿的变化；先更新快照再按变化路径调用`enqueue_analysis()`，重复轮询不会重置分析计时。`knowledge_candidate_service.enqueue`按书合并5秒更新，采用变化路径与分块hash、模型、提示词版本复用结果；自动分析包含草稿和大纲，索引历史范围仍只接受完成/发表正文。独立任务持久化文件及分块进度、失败材料、模型task_ids；取消指定任务通过epoch和should_cancel阻止迟到提交，保留此前已验证候选。生命周期恢复将中断分析标为可重试并修复已提交提案凭据。软删除取消任务，彻底删除仍核验本书身份并仅删除其知识目录。

**页面与三栏联动**：左侧资料/实体切换，48px资料行仅保留名称、状态；选择资料默认进入该资料局部图。明确入口切回本书全景，节点一跳聚焦可返回上一范围。右侧资料详情/检索问答/待审核页签，浏览优先展示关联实体、字段、来源和依据。定义、提及、计划、明确关系分别切换，片段默认隐藏，图例默认收起。中栏不足600px时收起资料并用详情抽屉；窗口小于920px显示资料/图谱/详情切换且默认图谱。三主题共用设计token。

**图谱渲染与请求一致性**：Cytoscape按ID增量更新；节点坐标、缩放和平移按本书及全景/资料/实体范围缓存，返回恢复视口。首次未知元素布局，显式适配限制最高缩放避免少量节点过大；边标签仅选中或悬停显示。切书取消旧请求并清空旧内容，落地前验证project/key及请求序号；范围请求保留旧画面并清楚标明实际成功范围。失败停止读取，允许重试或返回上一成功范围，不将旧画面标成失败请求的新范围。

**证据与原文定位**：节点先加载 `source_location.evidence_id` 对应的定义依据，每次最多 6 条并可继续加载；跳转前回读证据并核对本书身份与内容 hash，保留卡片自身的起止行，不以整个分块替代区段。编辑器 URL 参数为 `path/line/end_line/evidence/hash`；编辑器再次检查原文版本，并将包含 frontmatter 的全文行号换算为正文选区。原文版本不匹配时拒绝旧定位。

右侧详情优先列出所选资料定义的实体，再列提及实体和文档节点；摘要去掉重复标题与列表标记，完整路径、索引和模型信息收进详情及“索引与任务状态”。

**统一检索与问答**：页面、上下文预览、对话、合同、写作管线、审稿和多步工作流共用服务。声明式 `retrieval_profile` 来自 Agent 能力注册表（history/planning/review/setting/continuity/foreshadow/teardown/off），不增加意图触发词。新增只读 `search_story_memory` 工具，检索不扩展写域。作者要求、细纲/合同、实体构成最多三条查询；六条证据、每文档两条；逐项裁剪到第 8 级 2400 tokens，权威必读材料保持更高优先级，多步下一步重读并重新召回。纯语言修改、Ghost Text 和确定性门禁默认 off。问答通过 `generation_service.stream_task` 实际流式生成，[n]引用编号和最终来源版本均验证；失效/非法引用触发 `invalidate_answer`，前端清除回答；模型不可用仍显示证据与原因。

| 方法 | 路径（统一 `/api` 前缀） | 作用 |
|---|---|---|
| GET | `/projects/{id}/knowledge` | 材料、索引/图谱统计、书身份、模型与任务状态 |
| GET | `/projects/{id}/knowledge/graph` | 按 `document/kinds/chapter_before/center` 取有界子图，`limit≤300`；`include_content` 默认 false；返回版本、文档成员映射与精确节点出处 |
| POST | `/projects/{id}/knowledge/sync` | 仅本书增量索引同步，AI分析使用独立extract接口 |
| GET | `/projects/{id}/knowledge/entities` | 独立实体目录，`q/kind/offset/limit`筛选分页 |
| POST | `/projects/{id}/knowledge/extract` | `paths/types/force/retry_job_id`，增量分析或重试失败材料，返回独立任务 |
| GET | `/projects/{id}/knowledge/jobs` | 本书抽取任务列表 |
| GET | `/projects/{id}/knowledge/jobs/{job_id}` | 阶段、文件/分块完成数、失败材料、候选数量及生成task_ids |
| POST | `/projects/{id}/knowledge/jobs/{job_id}/cancel` | 取消指定任务，保留已验证候选，阻断迟到提交 |
| GET | `/projects/{id}/knowledge/candidates` | 按pending/conflict/stale/processed等状态筛选，核验待审来源版本 |
| POST | `/projects/{id}/knowledge/candidates/review` | `items[{id,version,decision,value,entity_anchor,target_entity_anchor,lifecycle,chapter_start,chapter_end}]`；生成每文件一份Proposal差异和逐项失败 |
| POST | `/projects/{id}/retrieval/search` | 关键词或混合召回及出处/节点 |
| POST | `/projects/{id}/knowledge/ask` | SSE `evidence/delta/error/done`，证据与流式回答 |
| GET | `/projects/{id}/knowledge/evidence/{evidence_id}` | 校验新鲜度后回读原文依据 |
| GET/PUT | `/projects/{id}/vector/model` | 本书嵌入设置、继承及实际指纹 |
| POST | `/vector/model/prepare` | 后台下载校验固定本地模型并启用维护 |

**候选审核与提交一致性（2026-10-02）**：每项模型输出必须有连续逐字引句及全文`start/end/line_start/line_end/document_hash`，重复引句需要明确跨度。来源章节状态也参与失效核验。定义锚点以书籍、文件、实体类型和身份定义建立；同名或别名多命中不自动归属。候选按实体、字段或关系对象及章节交集识别冲突，非重叠章节作为状态演变。审核前列出现有值/建议值/依据/目标，作者修改或明确改变生命周期由审核记录保存。

知识页面可见时每5秒请求总览，任务进行中为1.8秒；`overview()`比较持久化路径/hash/状态快照，变化路径进入5秒合并调度，未变化材料不重复调用模型。抽取任务返回`current_document`、`document_progress[{rel_path,status,chunks_total,chunks_completed,cached_chunks,failed_chunks,error}]`及总体计数；进度来自实际处理，不以计时器模拟。不同材料或任务可单独取消与失败重试。模型不可用保留失败路径与零完成统计。

`review()`持有既有文件变更的按书锁，验证候选版本、来源hash、目标hash，将同目标选择合成一份完整差异，保留其他原文。计划与草稿进入相应实体的计划分区，已确认字段进入设定或状态演变；明确关系无已有落点时使用`设定/关系网络.md`。生成Proposal后候选为`staged`。`proposal_service.apply_proposal()`在提交锁内再核验来源和知识元数据，沿用文件变更引擎的目标版本核验、快照、原子写、journal及恢复。凭据成功后才由`reconcile_proposal()`更新为`applied`；补账失败返回`reconciliation_pending`，重试依据journal修复，不能重复写盘。批量按文件分别返回成功/错误；丢弃提案将关联候选重新开放审核。

实体定义校验绑定锚点、标题、原卡正文、字段和别名，排除独立managed计划/草稿记录；同批先追加人物计划再应用关系提案仍有效，身份内容变化或同名定义换序则失效。完整审核Proposal内容hash防止从收件箱删除标记、修改生命周期或把计划复制到记录外。Proposal保存作者审核payload，崩溃后可恢复丢失的staging事务，再按journal凭据补账。已确认无范围字段的后续修订替换原managed记录，避免同一当前字段出现多个有效值。前端按Proposal ID去重，每个目标只展示一次；可读差异折叠持久化元数据，完整差异始终可展开。

同一材料既是来源又是写入目标时，应用后全文hash必然变化。已应用候选的原依据可从其已核验journal同路径before-image读取，要求原版本hash、exact引句跨度、候选/Proposal/书籍绑定、提交afterhash与记录metadata全部一致；接口标记`archived`和`archive`，原`document_hash`不改成当前版本。前端以“审核时的原文依据”弹窗展示，打开当前文件不携带旧行号或hash。未应用/失效候选、审核和提交预检仍只接受当前文件版本，归档读取不放宽任何写入门禁。

**可重建的知识事实边界**：已应用知识使用`<!-- wb-knowledge {JSON} -->`和`<!-- /wb-knowledge -->`包裹可读正文，JSON含稳定知识ID、候选ID、provenance、review_status、reviewer、entity_anchor、field、lifecycle、chapter_start/end、evidence/sources。`draft/planned/setting_fact/historical_event`分别保存，模型来源不会因审核改成author。`knowledge_lifecycle.project_text()`隐藏元数据、未审核记录和历史场景中的草稿/计划，并保留原文位置；起始章不早于目标章的记录不能证明先前剧情，`setting_fact`仅在适用范围内生效，已发生事件可作为历史证据，状态直读按目标章前的状态范围筛选。分块按知识record body边界切分，避免计划与事实共用片段。设定第4级、状态第5级及伏笔第6级直读、图谱与历史检索共用过滤；自由旧Markdown保持兼容。原始大纲仅在planning检索及graph展示/证据读取可见，历史类检索排除大纲；上下文第2级仍将大纲作为明确写作计划注入。候选图谱使用`candidate:<id>`证据ID，证据接口重新核验原始材料hash与跨度后返回引句，不将候选建议文本当作原文证据。

本轮实现与验证记录见[知识库与图谱优化及AI知识维护验收](知识库与图谱优化及AI知识维护-验收记录-2026-10-02.md)。浏览验收入口`python -m workbench.backend.tests.knowledge_acceptance_server`使用`.workbench/knowledge-acceptance`中的原创临时书籍和明确标识的固定本地模型；不使用作者材料、正式数据库或远程模型。自动回归覆盖`test_knowledge_candidates.py`、`test_knowledge_lifecycle.py`、`test_knowledge_proposal_commit.py`及既有知识测试；具体实际运行结果以本轮验收记录为准。

小于920px的全站路由临时隐藏全局导航，顶栏以覆盖式抽屉访问导航，Escape/遮罩/关闭/导航切换/跨越布局阈值均可关闭；不修改宽屏的个人侧栏偏好。20个以内图谱节点的目标屏幕标签字号为14px，更大范围保持原策略，布局与范围缓存不变。

验收入口：`tests/test_knowledge.py`、`test_knowledge_graph_v2.py`、`test_knowledge_integration.py`、`test_knowledge_store.py`、`test_embedding_service.py`；前端 `tests/knowledgeGraph.test.mjs` 和 `tests/knowledgeWorkspace.test.mjs` 覆盖增量布局、书籍隔离、选文件/局部范围切换、证据高亮和定位。真实模型评测 `evals/knowledge/run_semantic.py`，24 条原创样例及逐项命中结果存 `cases.json`、`last-result.json`；不访问作者小说，不调用云模型。2026-09-28 本机验收：Recall@5 = 24/24（100%，门槛80%）、24 片段同步 2.18 秒、单次检索中位 193 毫秒、P95 218 毫秒、测试进程工作集 165 MB、峰值 201 MB。这是小规模合成语料指标，大书需另测。视频问题、真实材料复现和后续优化验收见《知识图谱视频对比与优化验收-2026-09-28.md》。依赖/模型许可先登记于 `third-party-notices.md`；本次视频体验修复没有新增运行依赖。

2026-09-29 视频修复后复核：完整后端 541 passed / 1 skipped（Windows 无符号链接权限）/ 2 条依赖弃用提示；最终 v2.2 定向图谱回归 32 passed；前端 44 passed，TypeScript 与 Vite 生产构建通过。真实项目 10 的 12 份材料 hash、51 条向量内容均与修复前一致。默认全景 42 节点/115 条各类连线；三主题、1366/900/560px 窗宽、材料联动、鼠标拖动缩放、原文精确选中和关键词检索均通过实际浏览器验证。最新本地 BGE 24 条评测仍为 Recall@5 100%，同步 3.184 秒、查询中位 298.1ms、P95 437.93ms，进程工作集 164.6MB/峰值 202.3MB；临时评测书和索引统一创建于 `.workbench/evals/` 后清理。上述小语料数据不代表大书上限，逐项结果见 `evals/knowledge/last-result.json`。

### 3.11 模型供应商与凭据

| 模块 | 职责 |
|---|---|
| `provider_service.py` | LLM 供应商 CRUD、baseURL 拉取模型列表、健康检查、启用；**`project_to_dsh_home()` 单向投影到独立 DSH_HOME** |
| `secret_store.py` | 凭据存储 `.workbench/secrets.json`（Windows DPAPI 加密）；不入 Git / 导出包 / 日志 / Prompt；UI 掩码 |
| `settings_service.py` | 工作台偏好设置 `.workbench/settings.json`（非事实源，可重建） |

前端模型供应商和生图供应商的 API Key 输入统一使用 `components/CredentialInput.tsx`，配套样式 `styles/credentialInput.css`。控件使用 `type="text"`、`autocomplete="off"` 与视觉掩码，移除原 `type="password"` / `new-password` 组合对浏览器登录凭据识别的触发；`new-password` 只能影响自动回填，不能作为禁止保存密码提示的保证。支持 `-webkit-text-security` 的浏览器逐字符掩码；其他浏览器隐藏输入字形并覆盖固定圆点，只有用户主动点击显示按钮时呈现本次输入的明文。离开控件、窗口失焦、页面可见性变化、值清空或禁用时回到隐藏态；显示按钮可通过键盘操作并带 `aria-pressed`。

编辑已存供应商时 `api_key` 始终初始化为空，后端返回的掩码只作提示，不回填为输入值。留空保存继续使用原 Key，填入新 Key 才替换；本轮输入仍按既有 `secret_store` 流程保存，未改变存储位置、加密或接口契约。`lib/autofill.ts` 继续为普通配置字段提供关闭自动填充和第三方密码管理器忽略标记；浏览器设置及扩展的自主行为不由应用静态检查控制。

### 3.12 生图工坊

| 模块 | 职责 |
|---|---|
| `image_provider_service.py` | 图片供应商 CRUD、解析、健康检查（**不投影 dsh**） |
| `image_adapters.py` | 图片模型适配器注册表：`ImageGenRequest`、`BaseImageAdapter`、`GPTImageAdapter`、`ADAPTERS`、`resolve_adapter()`、`calc_size()` / `parse_ratio()` / `BASE_RESOLUTIONS` |
| `image_service.py` | 任务编排：任务生命周期、图片落盘 + sidecar JSON、画廊记录、参考图暂存、`reindex()` 扫盘重建 |

**扩展新图片模型的唯一落点**：在 `image_adapters.ADAPTERS` 列表**头部**插入新适配器类（列表顺序即匹配优先级）；未命中时回落到 OpenAI 兼容的 `GPTImageAdapter`。

**事实源**：`.workbench/images/`（图片 + sidecar JSON + 参考图暂存）；索引：`image_providers`/`image_jobs`/`image_records`（可由 `POST /api/images/reindex` 重建）。

### 3.13 技能、Agent、规则、路由与工作流

| 模块 | 职责 |
|---|---|
| `skill_service.py` | 源技能同步、注册、启停、导入、AI 辅助生成；`compile_skills()` 按优先级去重完整注入命中正文，回传加载项、估算 token 和未加载原因；`skill_digest()` 保留兼容调用但忽略旧预算参数；引用按需完整读取；列表/详情/同步含 `estimated_tokens`，引用另含 `references_estimated_tokens`；同步覆盖引用并逐文件 prune |
| `agent_service.py` | Agent 定义存储于 `agents/<name>.yaml`；内置 Agent 标记 `is_builtin` 不可删除（可复制）；**声明式能力注册表**：`materials`（主责材料，取值 `MATERIAL_DIRS = (章节, 设定, 大纲, 状态, 备忘录)`）、`capabilities`（`{intent, triggers, skill, task_type}`）、`boundaries`；注册表查询 `capability_index()`/`agents_by_material()`/`describe_candidates()`/`compose_system_prompt()`；内置定义带 `version`，`ensure_builtin_agents()` 在磁盘版本更低时**幂等升级**（重写标题/描述/提示词/技能/工具/材料/能力/边界/版本，保留作者改过的 `provider_id`/`model_id`/`enabled`/`params`；作者自建同名文件绝不覆盖） |
| `intent_service.py`（**新增**） | 两段式意图路由实现：关键词快路径 + LLM 语义判定 + 确定性回退；产物含 `write_targets`/`read_only_refs`/`plan`/`skills`/`source`；详见下「两段式意图路由」 |
| `routing_service.py` | 路由**门面**：`classify()`/`route()`/`describe()` 语义兼容，内部委托 `intent_service`；`describe()` 的意图表来自 `agent_service.capability_index()`；**原硬编码 `INTENTS` 常量已删除** |
| `rule_service.py` | 规则全局/作品级 + 优先级链解析 + 编译产物 |
| `workflow_service.py` | 工作流引擎：可视化编排持久化（`workflows/*.json`）、批量产章、检查点断点续跑（`workflow_runs`） |

**声明式能力注册表（扩展路径）**：Agent 名单、主责材料、触发词、绑定技能与工具白名单**全部来自 `agents/*.yaml` 与 `agents` 表**，路由与编排代码中不存在硬编码的 Agent / 意图 / 技能名清单（`intent_service.intent_index()` 由注册表摊平而成）。新增/上传 Agent 填写 `materials` 与 `capabilities` 后立即进入关键词快路径与 LLM 候选清单；未知主责材料或未知工具在保存时被拒（`_validate_materials()` / `_validate_tools()`，工具白名单 `TOOL_WHITELIST = (检索, 文件读写, 收件箱, 审稿门禁, 向量检索, 无)`）。**本次未新增数据库列**：`agents` 表仍只存既有字段，`materials`/`capabilities`/`boundaries` 的事实源是 `agents/<name>.yaml`。

**内置 Agent（8 个，`version = 3`；`teardown-analyst` 为 `version = 4`）与技能绑定**（`material = []` 表示不写书稿）：

| Agent | 角色 | 绑定技能 | 主责材料 | 触发词要点 |
|---|---|---|---|---|
| `setting-keeper` | 设定（静态设定维护） | `novel-setting` | `设定` | 设定、世界观、势力、人物设定、设定卡 |
| `planner` | 规划（大纲与章纲） | `novel-planning` | `大纲` | 大纲、细纲、卷纲、章纲、卡文追问 |
| `writer` | 写作（章节正文） | `novel-writing` + `human-linguistics` | `章节` | 写这章、续写、写正文、新建章节 |
| `reviewer` | 审稿（逐项验收，**只读**） | `novel-review` + `human-linguistics` | — | 审稿、验收、挑毛病、去 AI 味 |
| `context-keeper` | 连续性（状态与追踪） | `novel-setting` + `character-arc` | `状态`、`设定` | 角色状态、时间线、资源账本、伏笔、角色成长 |
| `teardown-analyst` | 拆书对标（**只读**） | `novel-teardown`（`version = 4`） | — | 拆书、拆解、对标、爆款分析 |
| `writing-assistant` | 创作助理（`DEFAULT_AGENT`，兜底） | `human-linguistics`（**由原 4 技能收缩为 1 个**） | — | 未命中触发词时只读兜底；写任务须由拥有材料域的角色执行 |
| `chief-editor` | 总编（路由咨询，**只读、不执行**） | `novel-review` + `human-linguistics` | — | 兼容保留：`chat_run_service` 在未钉住 Agent 时把它换回 `writing-assistant`（见 §8.4） |

**两段式意图路由**（`intent_service.classify()`，无 `project_id` 或关键词已唯一命中时**绝不调用模型**）：

1. **关键词快路径（`source="keyword"`）**：触发词聚合自 `capability_index()`，零模型调用；判定前先**归一化文本**（去空白、全角转半角、英文小写）。快路径会做**显式材料扫描**：跟依据介词（根据 / 按 / 依据 / 基于 / 参考 / 照着 / 结合 / 沿用…）或位置限定（「设定里的…」）点名的材料只进 `read_only_refs`；显式点名的产出材料并入 `write_targets`（剔除只读依据与自由便签区 `备忘录`）。只读角色（`reviewer`/`teardown-analyst`/`writing-assistant` 等 `materials == []`）**恒不获得写域**。
2. **LLM 语义判定（`source="llm"`）**：仅在关键词**未唯一命中**、有 `project_id`、`chat.intent_llm` 未显式关闭、且判定引擎（`direct-api`）可用时调用（`task_type="意图判定"`、`temperature=0`、超时 20s）；候选清单由 `describe_candidates()` 动态生成，要求严格 JSON；未知 Agent 名 / 材料 / 计划步一律丢弃（`_normalize_*`）。
3. **确定性回退**：任何异常 / 非法 JSON / 低置信 / 未知角色名都吞掉并回退——顺序为 **钉住 Agent（`override`）→ 关键词结果（`keyword`）→ 默认执行者（`writing-assistant`，`fallback`）**，并在 `basis` 写明回退原因。**低置信口径**：`confidence` 低于 `LLM_MIN_CONFIDENCE = 0.4`（与 0.7/0.4 档位下界对齐）即丢弃判定改走关键词结果，`confidence` 缺失 / 非数值按 0 处理（同样回退）；该路径 `source="keyword"`（关键词也未命中则 `fallback`）、`llm_used=False`（与「未知角色名」回退同口径）。

**多步协作计划（`plan`）**：仅当作者**显式点名 ≥2 类产出材料**时生成（Agent 自带多材料不再触发假分步），每类材料一步、**最多 3 步**；每步含 `agent` / `write_targets` / `read_only_refs`（填前序步骤产出材料）/ `note`。`chat_run_service._prepare()` 消费计划：单材料不产生多步；多步时按步构建独立的 system / context / scope / 工具清单，`_execute()` **按步串行接力**（逐步调用 `runtime().stream()`），每步只写该步声明的材料，并 emit `kind="plan_step"` 的 `step` 事件供前端展示与刷新回看。

**技能渐进披露（L1/L2/L3）与用量口径**：

- **L1 元数据**：frontmatter 的 `name`/`description`/`appliesTo` 常驻（dsh 发现层只看 `name`/`description`）；
- **L2 正文**：`SKILL.md` 按命中逐轮完整注入，不裁剪；`compile_skills()` 仅读取命中正文，按优先顺序去重，逐项记录正文估算 token；
- **L3 引用**：`skills/<name>/references/**.md` 默认不注入，同步到 `.dsh/skills/<name>/references/`，模型按需调用 `read_skill_reference` 完整读取，仍限定在该技能的引用目录内。

估算使用既有 `estimate_tokens()`，不代表供应商真实账单。技能列表/详情/同步展示正文估算，引用另列；未读取引用不计入本轮技能注入量。对话步骤显示加载名称及约 N token，并随运行事件、快照和历史消息元数据保留。原生引用工具只在 `data` 返回正文，避免同一引用被序列化两遍。

**工具可见性与确定性校验工具**：`chat_workspace_tools.list_specs(read_only, agent_tools)` 只按本轮 Agent 的 `tools` 白名单（`TOOL_WHITELIST_MAP`）与只读状态过滤，**写工具不因不在写域内而消失**（模型仍能发起越界申请）；任何一轮都保留 `ALWAYS_AVAILABLE_TOOLS = ("ask_user_question", "read_skill_reference")`。新增只读工具：`read_skill_reference`（读技能 `references/`）、`check_tracking`（跑 §2.6 的三道确定性校验门，`target ∈ 账本/大纲/设定`，只报告不改写）与 `list_teardown`（拆书资产召回：给定 `target` 列该目标事实卡，否则按 `genre` 题材召回；归「检索」分组，异常降级不阻断）；两条对话路径（原生 `chat_workspace_tools` 与兼容 `chat_tools`）均可用。

**当前受管资产**：技能 8 个（`character-arc`、`foreshadow-check`、`human-linguistics`、`novel-planning`、`novel-review`、`novel-setting`、`novel-teardown`、`novel-writing`），每个 `SKILL.md` 为 7 节契约结构（触发条件 / 输入契约 / 输出契约 / 边界 / 步骤 / 正反例 / 验收清单）+ `references/`（共 9 份：`novel-setting` 2 份，其余技能各 1 份）；Agent 8 个 YAML（见上表，内置定义 `version = 3`，`teardown-analyst` 为 4）；工作流 1 个（`默认八步产章.json`）；模板 1 个（`默认模板`）；编译规则技能 `.dsh/skills/workbench-rules`（由 `POST /rules/compile` 生成，未编译时不存在）。

**工作流持久恢复**：`start_run()` 将流程定义冻结到 `payload.workflow_definition`；章节列表与 `completed` 是恢复依据，失败章再次执行，旧 cursor 跳过未完成章节时回到最早缺章。pending/paused/failed/interrupted 可显式续跑，同一运行由进程内锁拒绝并发。应用启动的 `recover_interrupted()` 将遗留 running 置为 interrupted，保留检查点且不自动生成。暂停优先于失败策略；允许继续后仍有缺章时整体为 failed。

自定义节点保存 `payload.node_checkpoints[chapter][node_id]`，含节点指纹、提案 ID、内容 hash 和成功状态。续跑核对提案项目/状态/内容，跳过已有有效节点；提案删除、丢弃或手改时明确阻断并提示核对后新建运行。每节点的 system、instruction、指定技能正文和已核验前序提案实际进入生成请求；所选技能不可用时节点失败，不调用模型。内置八步流程沿用 `pipeline_service.run_pipeline(resume=True)` 的自身检查点。

**技能与 Agent 绑定可见性**：`GET /api/skills`（见 §5.6）仍附 `agents`、`suggested_agents`、`missing_references`。绑定技能缺失、停用或格式无效时跳过该项并报告原因，不阻断其它有效技能加载。

**必注入技能与兜底注入**：清单仍为 `settings.writing.always_inject_skills`（默认 `["human-linguistics","novel-writing"]`）。`_round_writes_chapters()` 的写作轮次判定不变；命中时将清单并入本轮技能，通过 `compile_skills(priority_names=...)` 优先完整加载。单步及多步任务使用实际编译结果生成技能步骤；仅缺失、停用、格式错误产生未加载原因，没有超量或截断告警。

**落盘前自动去味（`settings.writing.auto_deslop`）**：`chat_workspace_tools._write()` 在首次 `preview_change()` 之前调用 `_maybe_deslop_chapter()`——仅当目标是 `章节/` 且操作为 `create`/`rewrite`/`replace_exact`，先预览一次候选；若 `禁词门`/`去AI味门`/`格式门` 失败且开关为真，则按 `human-linguistics` 重写（`generation_service.run_task`，`task_type="去AI味重写"`）并重跑门禁，最多 `max_rounds` 轮（硬顶 `MAX_DESLOP_ROUNDS=3`）。**重写后的文本即被预览、审批、落盘的那一份**；重写失败或仍不过门禁则原样交回门禁按既有语义拒绝落盘，并把经过写成 `context_warning`。

**对话回复体检**：run 正常结束时 `chat_run_service.check_reply_quality()` 对回复文本做轻量体检——仅当回复含 ≥300 汉字且存在 ≥150 汉字的连续段落（疑似粘出正文）才跑 `density_gate` 与 `novel_format`，命中时以 `context_warning` 落事件；下一轮 `_prepare()` 取上一轮该类事件，把「正文请用写工具落盘」的修正要求注入 `context_text`。

---

## 4. 前端架构

### 4.1 分层

| 目录 | 职责 |
|---|---|
| `src/main.tsx` | 挂载入口；Provider 顺序 `StrictMode → BrowserRouter → ToastProvider → SettingsProvider → App`；样式导入顺序敏感（`tokens → theme → global → app → workbench → chatWorkspace → editorWorkspace`） |
| `src/App.tsx` | **唯一路由表**（外层 `AppShell` 布局路由） |
| `src/pages/` | 路由页面（含新增 `Chat.tsx`；重型页面按路由 lazy 加载） |
| `src/styles/dashboard.css` | 仪表盘专用样式：统计工具条、指标、趋势/预算分栏、用量分布与任务表；颜色复用既有主题 token，按页面容器宽度调整布局 |
| `src/components/` | 工作区、对话、编辑与资料组件；`WorkspacePage` 统一页面外壳及资源状态 |
| `src/state/` | `useSettings`（后端设置）、`useToast`、`usePanelWidth`（面板宽度持久化）、`useAppearance`（护眼变量落地） |
| `src/theme/useTheme.ts` | 主题三态 `light/dark/paper`，写 `<html data-theme>`，存储键 `aiw.theme` |
| `src/lib/` | 逻辑与通用助手：`chatState.ts`（对话状态归并/校验，含路由卡归并 `routingCard()`，测试重点）、`chapterName.ts`（章节显示名）、`autofill.ts`（防浏览器凭据回填）、`clipboard.ts`（用户点击内同步复制与焦点/选区恢复）、`dashboardState.ts`（连续日期补零、图表刻度上界、任务类型与状态中文标签） |
| `src/api/` | `client.ts`（fetch 薄封装 + 全部端点，**按注释分组、扁平命名导出**）、`types.ts`（后端响应类型，保留后端中文键） |

### 4.2 路由与页面

| 路径 | 页面 | 主要功能 | 主要接口域 |
|---|---|---|---|
| `/` | `Bookshelf.tsx` | 书架：项目列表/开书（含模板）/归档/删除整本书（进回收站，可恢复或彻底删除）/回收站（整本书 + 文件条目）/导入导出/封面/发布导出 | projects、templates、trash、transfer、publish |
| `/workflows` | `Workflows.tsx` | 工作流编排、批量产章、暂停/恢复/断点续跑、运行记录 | workflows |
| `/inbox` | `Inbox.tsx` | 收件箱：逐条看 diff 后应用/丢弃、批量应用 | proposals |
| `/dashboard` | `Dashboard.tsx` | 7/30/90 天与书籍筛选、四项用量指标、连续每日趋势与预算、书籍/任务类型占比、最近任务详情与取消、CSV 导出 | usage、tasks |
| `/images` | `ImageStudio.tsx` | 生图工坊（全局）：画廊/收藏/灯箱、生成条、尺寸计算、供应商配置 | images |
| `/settings` | `Settings.tsx` | 全局设置 12 分区：模型供应商/引擎路由/外观护眼/编辑器（含「每章字数区间与去 AI 味」）/对话偏好/模板管理（`TemplateManager`：模板列表与内置只读、新建·复制·重命名·删除·导入导出、模板内文件树与内容编辑、默认模板与题材/平台适配；支持 `?section=templates` 深链）/预算用量/技能（卡片式列表，含必注入清单勾选、被哪些 Agent 绑定、未同步与缺引用提示、未绑定建议、引用文件数）/Agent（可编辑主责材料与边界声明，`capabilities` 只读展示）/规则/向量检索/关于与隔离 | providers、engines、settings、skills、agents、rules、vector、usage、templates |
| `/project/:id/chat` | `Chat.tsx` | 创作主入口；项目共享会话、正文目标、参考与选区、材料及实际交付 | chat、tree、chapters |
| `/project/:id/editor` | `Editor.tsx` | 三栏正文编辑器：文档树 + 正文（编辑/预览/阅读，章节走纯文本渲染）+ 右侧 `ChatPanel`；合同/八步循环抽屉；冲突检测；局部操作；幽灵文本；快照；字数区间提示（`getWritingPrefs`，低于下限/高于上限分别标红/告警） | tree、chapters、contracts、pipeline、gates、context、writing |
| `/project/:id/outline` | `Outline.tsx` | 灵感追问 → 累计生成 / 比较候选 → 候选对话优化 → 锁定 → 冻结 → 滚动规划 | outline |
| `/project/:id/bible` | `Bible.tsx` | Story Bible 分栏、伏笔台账、时间线、来源待标注、角色状态机/记忆/成长弧线 | bible、characters、ingest |
| `/project/:id/cards` | `Cards.tsx` | 设定卡片：卡片视图/高亮色/双向同步/章节行定位/导出 | cards |
| `/project/:id/knowledge` | `Knowledge.tsx` | 知识库与图谱：资料/实体联动、局部图与全景、检索问答、待审候选与抽取任务、证据原文定位（实现详见 §3.10） | knowledge、retrieval、vector |
| `/project/:id/teardown` | `Teardown.tsx` | 拆书资产库：导入 → 六维拆解 + 双时间线 + 事实卡 → 按题材召回 | teardown |
| `/project/:id/settings` | `BookSettings.tsx` | 独立项目默认设置：新会话权限/仅讨论、章节字数区间；保存失败保留输入；保存后通知当前项目控制器刷新默认值 | chat-defaults、writing-prefs |
| `/project/:id/review` | `Review.tsx` | 审稿中心：逐项审稿/硬门禁定位/质检矩阵/质量债/文风指纹/去 AI 味软审/摄取 | review、debts、style、ingest |
| `*` | `NotFound.tsx` | 兜底 | — |

导航分组与标题映射在 `src/components/AppShell.tsx`。本书按「创作、资料、检查」分组，全局工具独立分组，收件箱恢复常驻入口。真实书名与切书下拉取自项目列表；书架主动作进入 `/project/:id/chat`，裸项目路径重定向到 `chat`。所有原有业务路由继续可直接访问。

### 4.3 API 客户端（`src/api/client.ts`）

- `API_BASE = '/api'`（开发期走 Vite 代理，不硬编码主机端口）。
- `request<T>(path, options)`：统一 JSON 编解码；`204` 与空体返回 `undefined`。
- `ApiError`：`status`（`0` 表示网络层失败，如后端未启动）+ 可选 `code`；`toApiError()` 从 `detail → message → code` 提取消息。
- `qs(params)`：跳过 `undefined/null/''`。
- 二进制特例不走 JSON：`uploadCover`、`uploadImageRef`。
- **SSE**：`readSSE(path, body, onEvent, signal?, method='POST')` — `fetch` + `getReader()` + `TextDecoder('utf-8')`，按空行切分事件、聚合 `data:` 行后 `JSON.parse`；支持跨块中文、注释行、末尾未终止事件。对话事件流用 `GET /chat/runs/{runId}/events?after=`（`streamChatRun`）。

### 4.4 关键组件

| 组件 | 职责与关键点 |
|---|---|
| `AppShell.tsx` | 布局壳：可折叠/可拖宽左侧导航、顶栏标题、主题切换；当前项目 id 持久化于 localStorage（`aiw.currentProjectId`），项目路由写入、全局页回退读取，切页不丢项目上下文 |
| `ChatPanel.tsx` | 纯展示助手：会话标题/历史/新对话与会话操作常驻；当前会话设置独立 Drawer，本书默认设置在 `BookSettings`；结果/计划/交付/待回应优先、技术详情折叠；宽材料区预览原文版本与差异，窄区使用 Drawer |
| `useChatController.ts` | 项目父路由持有唯一控制器，负责请求受理、冻结请求、草稿 revision、SSE 与快照合并、回应/停止/撤回。只有持久化助手终态消息才能确认结果同步完成；用户消息与 pending 占位不能清除临时结果 |
| `ChatHistoryMessage.tsx` | React.memo 隔离历史渲染，动作从控制器 ref 获取最新回调；流式增量集中在活跃消息 |
| `ChatInteractionCard.tsx` | 渲染批准类（含 DiffView）与提问类（单选/多选/「其他」）交互卡；批准卡提供「批准这次操作」/「本任务内不再询问」（`{decision:'approve', scope:'task'}`）/「拒绝」；**写域越界**时改为标注越界范围与目标材料，并提供「仅此次批准」（`scope:'once'`）/「本任务内允许改这类材料」（`scope:'material'`）两个范围选项 |
| `ChatMemoryPanel.tsx` | 本书对话记忆弹窗：更正、删除、来源跳转 |
| `DocumentTree.tsx` | 编辑器左栏文档树：分组、内联新建/改名、拖拽移动、状态徽标 |
| `MarkdownView.tsx` / `markdownRendering.ts` | 锁定 `react-markdown@10.1.0`、`remark-gfm@4.0.1`；标准 GFM 表格/列表/代码；不执行 HTML，仅精确 `<br>` 安全换行；默认安全 URL 过滤；明确来源语境转换中文标签；`.txt` 用 `plain` 原文装饰模式 |
| `ChapterEditor.tsx` / `novelDecorations.ts` | CodeMirror 6 行号、软换行、撤销与精确选区定位；名称颜色、闭合中文对话斜体与字数节点；三模式共用 UTF-16 原文位置；组合输入延后装饰和保存；局部替换核验原选区；章末追加核验完整正文并定位、聚焦 |
| `DiffView.tsx` | 行级差异视图（默认只显示变更附近 6 行上下文） |
| `PanelResizer.tsx` | 面板分隔条：拖拽期只回调 `onPreview` 直写 CSS 变量（不触发重渲染），松手落状态；键盘 ←/→ 每次 16px |
| `Modal.tsx` / `ConfirmDialog.tsx` | 通用遮罩弹窗 + Promise 化的 `useConfirm()`（替代 `window.confirm`）；用于**必须决策**的冲突、多字段任务级表单与**不可逆**删除确认；`Modal` 的可选 `className` 扩展面板布局，仍复用 Portal、覆盖层栈与焦点管理 |
| `CredentialInput.tsx` | 本机 API Key 输入：普通文本控件配视觉掩码与显示/隐藏按钮，编辑时不回填已存 Key；模型及生图供应商共用，详见 §3.11 |
| `Drawer.tsx` | 右侧抽屉：与 `Modal` 同构的 Esc / 遮罩关闭与焦点管理，用于只读 / 长内容（差异、历史、产物、运行详情、文件预览）与结果型表单，避免居中遮罩遮挡上下文 |
| `InlineEdit.tsx` | 就地行内编辑：Enter 提交、Esc 取消、不做 blur 提交；用于「只为取一个名字」的场景（改名 / 新建 / 复制） |
| `useToast`（`state/useToast.tsx`） | 轻提示：`push(message, kind?, action?)`；第三参 `{ label, onAction }` 提供「撤销 / 去收件箱」等动作（带动作时 8s 消失），用于可逆操作的「直接执行 + 撤销」 |

### 4.5 构建与测试

- `vite.config.ts`：dev `127.0.0.1:5173` + `strictPort`，`/api → http://127.0.0.1:8790` 代理；build `outDir=dist`、`emptyOutDir`、无 sourcemap。
- `package.json`：`dev` / `test`（`node --test tests/*.test.mjs`）/ `build`（`tsc && vite build`）/ `preview`。
- 前端测试 `workbench/frontend/tests/chatState.test.mjs`：用 `node:test` + TS `transpileModule` 动态导入，覆盖 SSE 分块解析、事件幂等与迟到快照、交互修订与选项校验、权限迁移、上下文裁剪、若干 API 请求体断言。
- 仪表盘纯函数测试 `workbench/frontend/tests/dashboardState.test.mjs`：沿用 `node:test` 与 TS 动态导入，覆盖连续日历补零、跨月/闰日、服务端日期锚点、1/2/5/10 图表刻度及任务中文标签；通过既有 `npm test` 入口执行。

### 4.6 交互呈现规范（弹窗使用纪律）

按「交互承担的职责」选择形态，而不是一律弹窗：

| 场景 | 形态 | 组件 |
|---|---|---|
| 只为取一个名字（改名 / 新建 / 复制） | 就地行内编辑 | `InlineEdit`（或组件内受控草稿态） |
| 只读 / 长内容浏览（差异、历史、产物、运行详情、文件预览、发布结果） | 右侧抽屉 | `Drawer` |
| 可逆的破坏性操作（移入回收站、进收件箱） | 直接执行 + Toast 带「撤销 / 去收件箱」 | `useToast` 的 `action` |
| 不可逆 / 高副作用破坏操作（彻底删除、删除模板 / 节点 / 拆解目标 / 供应商 / 技能 / Agent / 规则） | 保留确认 | `useConfirm` |
| 短表单（1~2 控件）与本地高频微调 | 行内展开 / 锚定展开面板 | 组件内受控态（如生图尺寸面板） |
| 任务级长表单与必须决策的冲突 | 保留居中遮罩 | `Modal` |
| 系统文件选择 / 下载 | 保留浏览器原生能力 | `<input type="file">` / `anchor.click()`，可另补拖拽落点与 Toast 回执 |

硬性约定：

- **禁止**新增原生对话框与 `window.open`；一律使用上表组件（`window.alert/confirm/prompt` 历史残留已清零，`PromptDialog.tsx` 已删除）。
- 守卫脚本：`python workbench/cli/check_no_native_dialogs.py` 扫描 `workbench/frontend/src/**/*.{ts,tsx,js,jsx,html}` 与前端 `index.html`，跳过 `node_modules` / `dist` / `build`。检查 `window/globalThis/self` 的点号或方括号原生对话框/新窗口调用、裸 `alert`、密码输入与 `new-password`、`beforeunload`、Async Clipboard、权限查询/请求、通知、媒体、定位、凭据/设备 API、文件系统权限 picker 与 `reportValidity()`；命中退出码 1。注释剥离保留字符串内 URL 与行号，多行调用可匹配。局部 `useConfirm()` 绑定不误杀，显式 `<input type="file">` 和下载链接继续允许。
- 拖拽落点统一用既有 `is-dragover` / `.drop-zone` 样式；`.is-dragover` 由组件状态驱动。
- 交互呈现改造的完整清单与逐场景结论见 [`.trae/documents/弹窗交互全面排查与友好化改造方案.md`](../.trae/documents/弹窗交互全面排查与友好化改造方案.md)。

**浏览器自动提示防线**：API Key 使用 §3.11 的 `CredentialInput`。卡片导出和回复复制统一调用 `lib/clipboard.ts` 的 `copyTextToClipboard(text)`，在用户点击栈内同步使用兼容复制命令，不调用 Async Clipboard 或请求剪贴板权限；完整保留中文、换行和长文本，临时输入框附着于当前对话框（否则为 body）以保持焦点约束，完成后清理并恢复焦点、输入选区、正文 Range 和选择方向。浏览器不支持或拒绝复制时仅显示失败 Toast，提示用户选择文本手动复制。`ChatInteractionCard` 的问题表单设置 `noValidate`，必填含义使用 `aria-required`，是否可提交仍由 `questionResponse()` 的选项/非空校验决定；不依赖浏览器原生校验气泡。

**模型配置呈现**：`Settings.tsx` 使用 `Modal` 的 `className="provider-editor-modal"` 和 `styles/settingsProviders.css`，面板最大宽度 1040px，连接配置、模型配置、拉取结果和可选能力标签分区展示。每个模型独立卡片，模型 ID、显示名称、上下文与最大输出按四列对齐，删除位于卡片标题栏，Token 预设用可按下按钮且再次点击清空。视口 ≤900px 时模型字段改为两列，≤560px 时连接、模型字段和拉取列表改为单列、缩小内距；头部与底部保留在面板内，仅正文区域滚动，底部保留取消/保存与编辑态健康检查。拉取动作移到模型分区，能力标签默认折叠。

**模型表单状态**：拉取中禁用重复拉取，重置/关闭/切换供应商时递增请求版本并清空拉取结果，迟到成功、错误或收尾不再更新新表单。全选只包含未添加模型，批量添加对选择和当前行按模型 ID 去重。保存以同步 ref 锁阻止并发提交，期间禁用字段和取消操作、拦截遮罩/Esc 关闭；成功重置表单后刷新列表，失败保留本次输入并用应用内提示说明。新建与编辑仍使用既有供应商 API。

静态守卫覆盖仓库自有源码中的直接调用，不能解析 API 别名、动态生成的名称、外部扩展或浏览器内部 UI；没有发现匹配不等于所有浏览器策略都已验收。显式文件选择/下载由用户操作触发，保留其系统能力。专项审计范围、实际回归和浏览器观察见 [模型配置与浏览器提示优化验收记录](模型配置与浏览器提示优化-验收记录-2026-10-03.md)。

---

### 4.7 项目共享助手与全站工作区（2026-10-03）

`/project/:id` 父路由使用 `ProjectChatWorkspace`，`ProjectChatProject` 直接持有唯一 `useChatController`，同一本书仅挂载一个展示用 `ChatPanel`，通过 Portal 在完整对话页、编辑器或覆盖面板呈现。`ProjectChatProject` 以项目 ID 为 key，切书重新创建以隔离材料和请求；离开项目取消订阅，服务端任务继续。`state/projectChat.tsx` 提供页面上下文注册、助手 host、打开/关闭及选区附件接口。上下文必须声明 `projectId`、`pageType`，描述项目内文件、选区、正文目标、未保存状态和保存/打开/刷新回调；跨项目注册被拒收，请求中的可选 `project_id/page_type` 只是来源描述，服务端会话仍是项目权限事实源。授予写域由后端处理。`usePageChatContext` 只主动提供当前文件作为参考，不将整页目录自动附入请求；未保存材料必须由页面实际保存（Editor）或明确阻止发送，不能以磁盘旧稿替代作者未保存内容。

草稿由 `chatDraftState.ts` 按项目/会话保存；发送持有版本号和完整请求快照。发送分为保存并核对材料、受理任务，网络重试复用相同请求 ID 与上下文；仅相同草稿版本在受理后清空。对话停止以返回快照为准；审批提交成功与后续状态读取分别处理。SSE 分类永久 HTTP 错误与暂时断流，30 秒无帧时核对快照并恢复订阅。

编辑器作者草稿由 `editorDraftState.ts` 按项目/文件保存到内存及浏览器 localStorage，离开、刷新和切书保留全文及原始 `ChapterDetail` 版本；最近文件按书保存，只恢复仍存在的文件。CodeMirror 历史按书/文件缓存，切页保留撤销与阅读位置。恢复时磁盘 hash 变化仍使用原草稿基线请求冲突检查，不将外部版本当作作者保存依据。保存回包只清除对应发送正文；期间继续输入的草稿保留并更新到已保存版本，离页后的回包也遵守此规则。章节状态更新只合并状态，不采纳新的磁盘正文或 hash；切书及切文件拒收旧响应。目录和正文独立显示首次读取失败与重试，刷新保持现有内容。localStorage 失败回落内存；刷新持久恢复依赖浏览器存储可用。这是未保存输入缓存，不替代文件、数据库或正式快照。

终态快照只有在会话助手消息包含对应 run 的已持久化终态结果后才从活动区移除，暂时滞后的 GET 不会令停止结果或交付消失。`ChatHistoryMessage` 用 memo 按消息对象、来源高亮、操作状态与只讨论权限变化判定；流式 delta 只更新活跃结果，切换只讨论仍会刷新历史撤回按钮。

`WorkspacePage.tsx` 统一页面骨架、分区标签、资源状态、书名选择及低频操作菜单。`useResourceRequest(scope)` 用范围与请求序号拒收过期响应；首次加载/空内容/筛选无结果/刷新/读取失败/操作状态分别显示，刷新失败保留上次成功内容。设置用分组侧导航，`?section=` 与浏览器前进后退双向同步。工作流分开定义、启动与记录，运行记录默认可读、JSON 折叠。

工作流启动按实际书籍加载章节，第一章、范围下拉、指定章节勾选三种方式均传真实正文路径；切书清除选择并拒收旧书章节。pending 有“开始运行”，状态为中文，HTTP 成功但执行失败时显示具体失败原因并保留续跑入口。仪表盘按项目列表映射真实书名。

设定分类的读取回调绑定当前分类；搜索覆盖名称、字段及依据，无匹配与无资料分开显示。成长曲线按项目和人物隔离，切人物清除旧图并拒收迟到结果，同一人物刷新失败保留内容。设置 Agent 的技能/供应商依赖分别展示状态和重试，编辑表单也使用该资源状态，首次失败不会显示“暂无可绑定技能”。拆书事实卡按目标/维度区分筛选空，产物按类型隔离；题材召回绑定项目、题材和条数，改条件清除旧结果，失败刷新保留相同范围的数据。

`useOverlayFocus.ts` 为 Modal、Drawer 和窄屏导航共用覆盖层栈：仅最上层接收 Esc/Tab，焦点约束在对话框内，背景 inert，关闭后恢复原焦点；组件通过 Portal 挂到 body，避免父级 overflow 或 transform 裁切。自动响应式布局与作者保存的折叠/宽度偏好分离。字号 token 为 UI 14px、对话 16px、阅读默认 18px；已保存的作者阅读参数继续通过 `applyAppearance` 覆盖。所有颜色/圆角/阴影/层级引用 token，操作图标用内部线条 SVG，不新增 UI 框架或运行时依赖。对话容器 ≥1000px 并排材料，编辑器容器 ≥1200px 三栏、900–1199px 目录 Drawer、<900px 助手覆盖层；入口始终可达。Modal/Drawer 使用深度与层级排序处理嵌套覆盖层，子详情关闭后焦点回到上级操作。

材料面板通过 `PanelResizer` 支持拖动/键盘调宽（260–560px，默认320px），`aiw.chat.materialsWidth/materialsCollapsed` 保存作者偏好；窄屏自动进入抽屉时不覆盖偏好，恢复入口与输入区材料入口常驻。原文预览含文件 hash/修改时间，刷新失败保留前一版本并允许重试。交付卡展示已保存/待应用/门禁未通过/未完成，差异宽屏就近展示、窄屏进入抽屉；撤回仍受后端共同权限守卫。

书架和书籍切换默认进入 `/project/:id/chat`。`BookSettings` 在 `/project/:id/settings` 管理本书新对话默认权限与章节字数，各资源独立加载/保存，保存后发 `aiw:book-defaults-changed` 通知共享控制器更新；当前会话配置只在对话设置中调整。

手动浏览器脚本 `tests/workbench.browser.acceptance.cjs` 与 `tests/chat.browser.acceptance.cjs` 不接入 CI：全部 `/api` 用两本合成书 mock，未知端点返回 503；既有运行时提供 Playwright，无新增项目依赖。性能基线从开工时保存的当前工作树源代码 archive 重建，与改造后相同负载对照。真实模型独立对照入口 `evals/chat-workspace/run_comparison.py`，隔离数据库/书籍/DSH_HOME，运行中输出预算/认证等失败按原始事件记录。完整结果见 [工作台整体改造验收记录](工作台整体改造-验收记录-2026-10-03.md)。

浏览器可设置 `WORKBENCH_ACCEPTANCE_DIST=workbench/frontend/dist`，由隔离浏览器路由直接提供生产资产，无需监听服务；所有 API 仍截获。全站脚本另支持 `WORKBENCH_ACCEPTANCE_FILTER` 与 `WORKBENCH_ACCEPTANCE_OUTPUT`，对话脚本 `--performance-only` 可顺序测量相同负载。真实评测启动前冻结双方源码、实际 Agent、共用技能/规则/流程/模板和评测工具并逐文件 hash；已有目录参数或源变化、冻结副本变化、执行状态不明时拒绝复用。schema 3 将锁定 vendor 作为显式运行时链接单独校验，不遍历复制外部目录；`--freeze-only` 不加载密钥或调用模型，但执行双方原生模块导入预检，避免因 vendor 路径遗漏把启动错误混入质量评测。离线检查脚本独立于 pytest/CI。执行 usage 与 SQLite 意图判定 usage 分开汇总，旧记录无逐轮边界时只提供 case 总量。第12组故障点在首步核验完成、第二步模型请求开始前，并记录收据与实际 hash；只有真正达到该点才算注入。供应商明确额度不足后停止发送剩余用例，未执行项保持缺失。

### 4.8 仪表盘呈现优化（2026-10-03）

`Dashboard.tsx` 将统计范围收拢为横向工具条：7/30/90 天分段按钮与全部书籍/单本书筛选；页头提供刷新数据与 CSV 导出。四项指标依次显示区间 Token 总量、调用次数、估算成本与今日 Token，数值和说明使用分层字号。书名通过项目列表映射，无法匹配时明确显示「当前不在列表中」，无项目归属的记录单独标注；用量、任务和书籍列表分别保留资源状态、失败提示与重试入口。

每日趋势与预算在宽容器中并排。`lib/dashboardState.ts` 的 `buildDailyUsage()` 以 `/usage` 返回的 `period.end` 为日期锚点，补齐当前区间每一天，缺失记录填零；`getChartCeiling()` 给出 1/2/5/10 幂级数上界，各柱共用带刻度的 Y 轴（页面最小上界为 10），空白日期仍占据对应位置。超过 14 天的图表缩小柱间距，避免 90 天列宽被内距挤没。鼠标悬停或键盘聚焦日期柱可查看该日明细，避免只凭相对柱高推断用量。后端 `token_service.summary()` 的近 N 天包含今日，起点为 `today-(N-1)`，截止今日并排除未来日期记录，响应显式带 `period: {start, end}`，不依赖浏览器当前日期推断服务端区间。

预算面板将每日上限 0 解释为「不限额」，告警阈值显示为百分比；设置上限后展示今日用量、上限及进度。千 Token 单价未配置时提供设置入口，历史估算成本继续使用每次调用落库的 `cost_estimate`，修改当前单价不会重算已发生调用。书籍用量表包含 Token 占区间总量的比例，任务类型按同一总量展示占比分布。

最近任务按书籍筛选，不随统计天数变化。用量与任务各自使用独立 effect，切换天数只刷新用量；任务区域默认取所选书籍最近 30 条，并提供单独刷新记录入口。表格合并展示时间/任务类型与书名、模型/引擎、中文状态、耗时及操作；常见类型与状态由 `formatTaskType()` / `formatTaskStatus()` 转换，未知值保留原文。任务详情继续使用 `Drawer`，显示 Prompt 标识/版本、执行结果与错误，完整上下文和任务记录折叠在详情内，折叠入口显式登记键盘焦点，无结果时显示占位说明；进行中的任务保留取消操作，CSV 由当前用量在本地生成并下载。

`Dashboard.tsx` 按路由导入 `styles/dashboard.css`，使用既有颜色、圆角和阴影 token，`.dashboard-page` 建立命名容器。容器 ≤950px 收紧内距与侧栏，≤780px 指标从四列变为两列、趋势/预算与书籍/类型分布转为纵向，≤540px 筛选工具条纵向排列、页头操作适配整行。表格窄屏在自身区域横向滚动，不扩大整页宽度。不引入图表库、UI 框架或其他运行时依赖，图表与分布直接使用 React 与 CSS。

## 5. 接口清单（HTTP API）

所有前缀为 `/api`（生图工坊为 `/api/images`）。以下为当前代码实际注册的端点。

### 5.1 `api/projects.py`（tags: projects）

| 方法 | 路径 | 用途 |
|---|---|---|
| GET / POST | `/projects` | 项目列表 / 新建项目（开书） |
| GET | `/projects/{project_id}` | 项目详情（顺带执行一次章节 `.md` → `.txt` 幂等迁移，迁移失败不阻断打开） |
| PATCH | `/projects/{project_id}` | 更新项目元信息 |
| DELETE | `/projects/{project_id}` | 删除整本书：默认移入回收站（软删除，可恢复）；`?permanent=true` 彻底删除（目录 + 全部索引记录，不可恢复） |
| POST | `/projects/{project_id}/restore` | 从回收站恢复整本书（目录搬回 `projects/{书名}/` 并清除软删除标记） |
| GET / PUT | `/projects/{project_id}/chat-defaults` | 读取 / 设置本书新会话默认权限 |
| GET / PUT | `/projects/{project_id}/writing-prefs` | 读取 / 设置本书每章字数区间（下限 < 上限且上限 ≤ 50000，非法值 400） |
| PUT / GET / DELETE | `/projects/{project_id}/cover` | 设置 / 读取 / 清除封面 |
| POST | `/projects/{project_id}/archive`、`/unarchive` | 归档 / 取消归档 |
| GET | `/projects/{project_id}/tree` | 读取文档树 |
| POST / PATCH / DELETE | `/projects/{project_id}/tree/node` | 新建 / 重命名移动 / 删除节点 |
| GET / POST | `/projects/{project_id}/chapters` | 章节列表 / 新建章节 |
| GET / PUT | `/projects/{project_id}/chapters/content` | 读取 / 保存章节正文 |
| PATCH / DELETE | `/projects/{project_id}/chapters` | 修改章节元信息（写 `chapters` 表）/ 删除（进回收站） |
| POST | `/projects/{project_id}/chapters/migrate` | 存量 `章节/第NNNN章.md` → `.txt` 幂等迁移（返回 `migrated`/`skipped`/`errors`） |
| GET | `/projects/{project_id}/trash` | 回收站列表（按项目过滤） |
| POST | `/projects/{project_id}/trash/restore` | 从回收站恢复（指定项目内条目） |
| GET | `/trash` | 回收站总表：整本书（软删除）+ 各项目的文件/文件夹条目，按删除时间倒序 |
| POST | `/trash/restore` | 跨项目恢复文件/文件夹条目（按 `entry.json` 的书名定位在架项目） |

### 5.2 `api/library.py`（tags: library）

| 方法 | 路径 | 用途 |
|---|---|---|
| GET / POST | `/templates` | 模板列表 / 由项目「另存为模板」 |
| POST / DELETE | `/templates/{name}/duplicate`、`/templates/{name}` | 复制 / 删除（内置不可删） |
| GET / PUT | `/templates/prefs` | 读取 / 写入默认模板与题材·平台映射 |
| POST | `/templates/blank`、`/templates/import` | 新建空白模板 / 导入模板包 |
| PATCH | `/templates/{name}` | 重命名自定义模板（内置拒绝） |
| GET | `/templates/{name}/tree`、`/templates/{name}/file`、`/templates/{name}/export` | 模板文件树 / 读文件 / 导出模板包 |
| PUT | `/templates/{name}/file` | 写模板文件（内置拒绝） |
| POST / PATCH / DELETE | `/templates/{name}/node` | 模板内节点 新建 / 改名·移动 / 物理删除（内置拒绝） |
| POST / GET | `/projects/{project_id}/index/rebuild`、`/index/stats` | 重建索引 / 索引统计 |
| GET | `/projects/{project_id}/search` | 全文检索（FTS，降级 LIKE） |
| GET / POST | `/projects/{project_id}/snapshots`、`/snapshots/restore` | 快照列表 / 恢复快照 |
| GET / POST | `/projects/{project_id}/export`、`/import` | 导出章节 / 导入章节文本 |
| GET / POST | `/projects/{project_id}/export/package`、`/projects/import/package` | 导出 / 导入整书包 |
| POST | `/projects/{project_id}/conflict/check`、`/conflict/resolve` | 外部修改检测 / 冲突解决 |
| POST | `/projects/{project_id}/tree/delete-batch` | 批量删除 |
| GET / PUT / POST | `/settings`、`/settings/reset` | 读取 / 更新 / 重置工作台设置 |
| GET | `/projects/{project_id}/log`、`/trash` | 项目操作日志 / 全局回收站 |

### 5.3 `api/models.py`（tags: models）

| 方法 | 路径 | 用途 |
|---|---|---|
| GET / POST | `/providers` | 供应商列表 / 新建 |
| PUT | `/providers/default` | 设默认供应商与模型 |
| DELETE / POST | `/providers/{provider_id}`、`/{provider_id}/enable` | 删除 / 启停 |
| POST | `/providers/health`、`/providers/models` | 健康检查 / 拉取模型列表 |
| POST | `/providers/project` | 项目级供应商绑定 |
| POST | `/providers/import`、`/providers/import-models-md` | 导入供应商 YAML / 从 `模型API.md` 导入 |
| GET / PUT | `/engines` | 引擎路由总览 / 更新全局引擎配置 |
| GET / PUT | `/projects/{project_id}/engines` | 项目级引擎配置 |
| GET | `/tasks`、`/tasks/{task_id}` | 生成任务列表 / 详情 |
| POST | `/tasks/{task_id}/cancel` | 取消任务 |
| GET | `/usage`、`/usage/recent` | 用量成本汇总（`days` 含今日，响应含 `period: {start, end}` 日期边界；成本采用落库估算值） / 近期记录 |

### 5.4 `api/writing.py`（tags: writing）

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/projects/{project_id}/context/preview` | 上下文包预览（构成/Token/优先级），支持 `include_style/style_query` 与实际样稿注入审计；不落审计 |
| GET / POST | `/projects/{project_id}/contracts`、`/contracts/generate` | 合同列表 / 生成合同 |
| GET / PATCH | `/projects/{project_id}/contracts/detail`、`/contracts` | 合同详情 / 修改（冻结后需 confirm） |
| POST | `/projects/{project_id}/contracts/freeze`、`/outline/freeze` | 冻结合同 / 冻结大纲 |
| POST | `/projects/{project_id}/outline/unfreeze` | 取消大纲冻结，无需请求体；返回完整 gates，其中 `outline_frozen=false`、`outline_frozen_at=null`，保留其他门禁及书稿；取消后 CHECK 的大纲前置条件恢复为未通过 |
| GET | `/projects/{project_id}/gates` | CHECK 门控（前置依赖与合同就绪） |
| POST | `/projects/{project_id}/pipeline/run` | 跑 8 步循环（可 resume） |
| GET | `/projects/{project_id}/pipeline/checkpoints`、`/checkpoint` | 断点列表 / 读取断点 |
| GET | `/proposals`、`/proposals/count`、`/proposals/{proposal_id}` | 收件箱列表 / 计数 / 详情 |
| PUT | `/proposals/{proposal_id}` | 编辑提案内容 |
| POST | `/proposals/{proposal_id}/review` | 审查当前待处理的管线章节候选，支持 `use_ai`，重绑当前版本审稿凭据；不应用正文 |
| POST | `/proposals/{proposal_id}/apply`、`/discard`、`/proposals/apply-batch`、`/discard-batch` | 应用 / 丢弃（含批量）；管线候选应用时核验当前版本审稿 |
| POST | `/projects/{project_id}/review`、`/review/deslop` | 章节审稿 / 去 AI 味软审 |
| GET | `/projects/{project_id}/reviews`、`/quality-matrix`、`/debts` | 审稿记录 / 质检矩阵 / 质量债 |
| POST | `/debts/{debt_id}/resolve` | 标记质量债已解决 |
| POST | `/projects/{project_id}/ingest` | 章节摄取（四件套） |
| GET | `/projects/{project_id}/timeline`、`/foreshadows`、`/ledger` | 时间线 / 伏笔台账 / 资源账本 |
| POST | `/projects/{project_id}/foreshadows/confirm` | 人工确认伏笔回收 |
| POST | `/projects/{project_id}/generate`、`/generate/stream` | 同步生成 / SSE 流式生成（可中断） |
| POST | `/projects/{project_id}/local-op`、`/ghost-text` | 局部改写（<500 字走 direct-api）/ 幽灵文本候选 |
| POST | `/projects/{project_id}/style/sample`、`/style/compare`；GET `/style/fingerprints` | 文风采样更新本章记录（note 省略保留、空值清空）/ 软比对 / 指纹列表（含有效性及重新采样原因） |
| PATCH | `/projects/{project_id}/style/fingerprints/{fingerprint_id}` | 取消本项目样稿认可（approved=false）；跨书 ID 返回不存在 |
| POST | `/proposals/{proposal_id}/rebase-state` | 来源正文仍匹配时，显式按当前角色状态材料重提完整候选；保留新人工字段，旧候选丢弃，新候选须重新核对差异，不应用正文 |
| GET | `/prompts` | 提示词注册表 |

### 5.5 `api/content.py`（tags: content）

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/projects/{project_id}/bible`、`/bible/unknown`、`/bible/{kind}` | Story Bible 概览 / 待核实条目 / 按类型列实体 |
| POST | `/projects/{project_id}/bible/source` | 标注条目来源 |
| GET / PUT | `/projects/{project_id}/outline` | 读取 / 保存大纲 |
| POST | `/projects/{project_id}/outline/expand`、`/candidates`、`/lock`、`/rolling` | 灵感扩展 / 累计追加候选 / 锁定 / 滚动规划；生成返回全量 `candidates`、总数 `count`、本批 `added_count` 与 `duplicate_count`；锁定优先接收 `candidate_id`，兼容旧 `index` / `title` |
| POST | `/projects/{project_id}/outline/candidates/{candidate_id}/refine` | 请求 `message`，以指定候选全文及其历史对话生成新版本；返回 `candidate`、全量 `candidates` 和 `count`，保留原候选、不自动锁定；空请求 / 空输出 / 无实质变化 / 目标候选冲突拒绝提交 |
| GET | `/projects/{project_id}/outline/history` | 大纲历史 |
| GET | `/projects/{project_id}/cards`、`/cards/categories`、`/cards/highlights`、`/cards/chapter-line` | 仅读缓存卡片及解析状态/任务标识；分类、有效对象三主题颜色、章节定位 |
| POST | `/projects/{project_id}/cards/refresh`、`/cards/highlight`、`/cards/generate`、`/cards/export` | 手动启动 AI 解析（`force` 强制重建）；颜色 `auto/manual/off`；智能生成经提案；导出 |
| GET / POST | `/projects/{project_id}/cards/tasks/{task_id}`、`/cards/tasks/{task_id}/cancel` | 后台解析进度 / 取消；重启恢复中断状态 |
| PATCH / POST / DELETE | `/projects/{project_id}/cards` | 编辑 / 新增 / 删除卡片 |
| GET | `/projects/{project_id}/characters` | 角色列表（含状态） |
| GET / POST | `/projects/{project_id}/characters/states` | 状态列表 / 写回状态 |
| PUT | `/projects/{project_id}/characters/field` | 更新深度设定字段 |
| GET / POST | `/projects/{project_id}/characters/memory` | 视角记忆列表 / 新增 |
| GET | `/projects/{project_id}/characters/{name}/growth` | 成长弧线 |
| GET / PUT | `/vector/model` | 全局嵌入模型信息 / 切换 |
| GET / PUT | `/projects/{project_id}/vector/model` | 本书嵌入设置（继承全局或 local/cloud/lexical）及实际模型指纹 |
| POST | `/vector/model/prepare` | 后台下载校验固定本地 BGE 模型并启用维护 |
| GET | `/projects/{project_id}/vector/stats`、`/vector/search` | 向量统计 / 检索 |
| POST | `/projects/{project_id}/vector/rebuild`、`/vector/index` | 重建 / 增量索引 |
| GET | `/projects/{project_id}/teardown`、`/teardown/facts`、`/teardown/recall`、`/teardown/artifact` | 目标列表 / 事实卡 / 按题材召回 / 读取产物 |
| POST | `/projects/{project_id}/teardown/import`、`/teardown/analyze` | 导入目标 / 六维拆解 |
| DELETE | `/projects/{project_id}/teardown` | 删除拆解目标 |
| GET | `/publish/platforms` | 发布平台清单 |
| POST | `/projects/{project_id}/publish/export` | 生成发布稿 + 自检清单 |

### 5.6 `api/orchestration.py`（tags: orchestration）

| 方法 | 路径 | 用途 |
|---|---|---|
| GET / POST / PUT / DELETE | `/skills`、`/skills/{name}` | 技能列表 / 详情 / 新建 / 更新 / 删除；列表返回 `{skills, estimated_tokens, target_root}`；单项含正文/引用估算、绑定信息、引用文件及缺失状态 |
| POST | `/skills/import`、`/skills/{name}/duplicate`、`/skills/{name}/enable`、`/skills/sync`、`/skills/generate` | 导入 / 复制 / 启停 / **同步到 `.dsh/skills/`**（含 `references/` 与文件级 prune）/ AI 生成 |
| GET / POST / PATCH / DELETE | `/agents`、`/agents/{name}` | Agent 列表 / 详情 / 新建 / 修改 / 删除。`GET /agents` 返回 `{agents, builtin_count, tool_whitelist, material_dirs}`；每个 Agent 含 `materials` / `capabilities` / `boundaries` |
| POST | `/agents/{name}/duplicate`、`/agents/{name}/model`、`/agents/draft` | 复制 / 绑定模型 / AI 起草 |
| GET | `/rules`、`/rules/resolved` | 规则列表 / 优先级链解析结果 |
| POST / DELETE | `/rules`、`/rules/{name}`、`/rules/compile` | 新建 / 删除 / 编译规则产物 |
| GET / POST | `/routing/intents`、`/routing/route` | 意图清单（来自能力注册表，含 `intent`/`agent`/`skill`/`task_type`/`triggers`）/ 意图路由。`POST` 另返回 `write_targets` / `read_only_refs` / `plan` / `skills` / `scope_unresolved` / `source`（`override`/`keyword`/`llm`/`fallback`）/ `llm_used` / `reason`，旧字段保留 |
| GET / POST / DELETE | `/workflows`、`/workflows/{name}` | 工作流列表 / 详情 / 新建 / 复制 / 删除 |
| POST | `/workflows/run` | 运行工作流 |
| GET | `/workflows/runs/list`、`/workflows/runs/{run_id}` | 运行列表 / 详情 |
| POST | `/workflows/runs/{run_id}/pause`、`/resume` | 暂停 / 恢复 |
| GET / POST | `/chat/sessions` | 会话列表 / 新建 |
| GET / PATCH / DELETE | `/chat/sessions/{session_id}` | 会话详情 / 更新 / 删除 |
| GET | `/chat/quick-cards` | 快捷卡片 |
| POST | `/chat/sessions/{session_id}/runs` | 发起一次对话 run |
| GET | `/chat/runs/{run_id}`、`/chat/runs/{run_id}/events` | run 状态 / 事件流（可回放，SSE 续接） |
| POST | `/chat/runs/{run_id}/cancel`、`/revert` | 取消 run / 回滚该 run 的文件变更 |
| POST | `/chat/runs/{run_id}/interactions/{interaction_id}/respond` | 回应批准或问答；批准可带 `{"decision":"approve","scope":…}`，`scope` 取值 `task`（本任务内不再询问）/ `once`（仅此次批准）/ `material`（本任务内允许改这类材料，写域越界卡的两个范围选项，见 §3.5/§6.3） |
| POST | `/chat/sessions/{session_id}/title/regenerate` | 重新生成会话标题 |
| GET | `/projects/{project_id}/chat-memory` | 书籍级对话记忆 |
| PATCH / DELETE | `/projects/{project_id}/chat-memory/{memory_id}` | 修改 / 删除记忆条目 |
| POST | `/chat/sessions/{session_id}/send`、`/stream` | 发送消息（非流式 / 流式） |

### 5.7 `api/images.py`（tags: images，prefix `/api/images`）

| 方法 | 路径 | 用途 |
|---|---|---|
| GET / POST / DELETE | `/providers`、`/providers/{provider_id}` | 图片供应商列表 / 新建 / 删除 |
| POST | `/providers/{provider_id}/enable`、`/providers/health` | 启停 / 健康检查 |
| PUT | `/uploads` | 上传参考图（原始字节，≤5MB） |
| POST | `/generate` | 发起生成（含 `input_upload_ids` 时走垫图/编辑） |
| GET | `/jobs`、`/jobs/{job_id}` | 任务列表 / 详情 |
| POST | `/jobs/{job_id}/cancel` | 取消任务 |
| GET / PATCH / DELETE | `/records`、`/records/{record_id}` | 画廊记录 / 修改（收藏、提示词）/ 删除 |
| GET | `/records/{record_id}/file` | 返回图片文件 |
| POST | `/reindex` | 扫盘重建画廊索引 |

### 5.8 `api/knowledge.py`（tags: knowledge）

知识库、图谱、候选审核、检索问答与统一检索的 13 个端点（`/projects/{id}/knowledge*` 与 `/projects/{id}/retrieval/search`）的唯一清单见 [§3.10 表格](#310-向量检索与索引)；本书级与全局向量模型设置端点见 §5.5，此处不重复维护。

---

## 6. 模块交互与数据流

### 6.1 三层数据流总图

```text
       写入                                   读取
UI/对话 ──► API ──► Service ──┬──► 事实源（Markdown/JSON，原子写） ──► 索引刷新 ──► SQLite/FTS/向量
                              └──► Engine（dsh / direct-api）──► 文本产物 ──┐
                                                                          ▼
                                          提案（Proposal）或变更日志（Journal）──► 人/权限批准 ──► 落事实源
```

**关键不变式**：

1. 任何 AI 产物**不得**直接写正文：走提案（管线）或变更日志（对话），应用前必须有 diff 与版本校验。
2. 索引永远是派生的：删 `.workbench/workbench.db` 后可全量重建，事实不丢（**例外**：章节标题/状态/合同ID 存在 `chapters` 表，删库前应导出或依赖 `.md` 迁移前的 frontmatter 快照）。
3. 写入一律原子写（临时文件 + rename）+ 章节/项目级锁。
4. 隔离：dsh 调用只经 `dsh_paths.py`，只注入独立 `DSH_HOME`，不读写用户全局 `~/.dsh`。

### 6.2 生成类请求（管线/生成）

```text
Editor / Review ──POST /generate/stream──► api/writing.py
   └─ generation_service.stream_task()
        ├─ context_service.assemble(project, chapter)        ← 9 级优先级 + 预算裁剪
        ├─ engine/router.pick_engine(task_type)              ← 三级覆盖 + 降级
        ├─ Engine 执行 ──SSE 增量──► 前端流式渲染
        └─ 产物写入草稿区/提案 ──► 收件箱（人工 diff 后应用）
                                       └─ 应用 → 快照 → 索引/向量增量 → 摄取（四件套）
```

### 6.3 对话类请求

```text
ChatPanel ──POST /chat/sessions/{id}/runs──► api/orchestration.py
   └─ chat_run_service.create_run()（幂等：client_request_id；唯一活跃 run 约束）
        ├─ ① 路由  routing_service.route() → intent_service.classify()
        │      关键词快路径（注册表触发词，零模型调用）→ 需要时 LLM 判定（意图判定 / temperature=0）
        │      → 校验归一（未知 Agent/材料丢弃）→ 失败回退（钉住 Agent → 关键词 → 默认执行者）
        │      产物：agent + skills + write_targets + read_only_refs + plan + source
        ├─ ② 组装上下文（本书记忆已确认项 + 参考文件 + 正文目标章节 + 本轮字数目标）
        │     └─ 命中章节写作轮次时兜底注入必注入技能（priority_names 保证完整）
        ├─ ③ 写域  chat_service.round_scope() → scope_guard.build_scope() → tool_context["scope"]
        │    （declared 为真才注入；未声明产出的轮次不注入，沿用既有权限语义）
        ├─ ④ 工具清单  list_specs(read_only, agent_tools)：按本轮 Agent 的 tools 白名单 + 只读状态过滤
        ├─ ⑤ 多步计划  有 plan 时按步构建独立 system/context/scope/工具清单，逐步串行接力执行
        ├─ engine/dsh_chat.DshChatRuntime ──worker──► 事件流
        │     session_ready / routing / step / delta / tool_call / tool_result / usage → …→ done|error|cancelled
        ├─ 工具桥（chat_workspace_tools.TOOLS）
        │     读：list_files / read_file / search_files / run_gates / check_tracking / read_skill_reference
        │     写：create_file / new_chapter / replace_text / write_file / move_file / delete_file
        │         ├─ 权限判定顺序：只读/仅讨论拒绝（deny）→ 写域 → 批准强度
        │         │     ask/auto 范围外 → 降级为批准卡（越界说明 + once/material 选项）
        │         │     full 范围外 → 默认放行并留痕；scope_limit_full 时改为请求批准
        │         │     scope_strictness=reject → 范围外直接拒绝；写 备忘录/ 永不判越界
        │         ├─ 章节候选先按 auto_deslop 自动去味重写（重写文本即落盘那一份）
        │         └─ file_change_service.apply_change()（版本校验 + 门禁 + journal + 章节元数据写库）
        │     每次工具调用前重读会话权限（permission_mode / discussion_only）→ 改权限即时生效
        └─ 需要人决策时：emit interaction ──► 前端卡片 ──► respond ──► 回填原工具调用结果
              （ask 模式可「本任务内不再询问」；越界授权的「本任务内允许这类材料」是另一个独立开关）
   ◄── GET /chat/runs/{id}/events?after=n（SSE 可回放，刷新/重连不丢）
对话消息 meta.routing 落库（含 plan / write_targets / scope_label），刷新后可回看路由卡
结束时 check_reply_quality() 对回复做轻量体检，命中写 context_warning → 下一轮注入修正要求
revert ──► file_change_service.revert()（按 before-image 还原）
```

`ask_user_question` 提供 1–3 个问题（单选/多选/固定「其他」），等待作者**不消耗**模型执行超时。

### 6.4 写入与撤回

```text
对话写工具 / 提案应用
   └─ file_change_service
        ├─ managed_path()        路径边界（本书可见文档；章节限 `章节/第NNNN章.txt`；隐藏/跨书/链接绕过拒绝）
        ├─ assert_version()      expected_hash 与磁盘比对 → 过期抛 FileConflictError
        ├─ 门禁（章节整章）      不过 → 抛 GateRejectedError，不落盘（自动去味重写环节会先尝试修正）
        ├─ 章节正文规范化        `_normalize()` 只留纯正文；落盘后 `_sync_chapter_meta()` 写 `chapters` 表
        ├─ 记录 journal          before-image 自持，可 revert
        └─ snapshot_service      保存/应用前存档
```

### 6.5 引擎选择与降级

```text
task_type ──► router.TASK_ROUTING 决策表 ──► resolve_route(项目级/任务级覆盖)
   ├─ 首选 dsh-headless：available()? ──是──► DshEngine（子进程，经 dsh_paths + 独立 DSH_HOME）
   └─ 否 / 不可用 ──► DirectApiEngine（OpenAI 兼容，错误码归一化 + 重试分类）
```

### 6.6 上下文组装数据流

```text
context_service.assemble()
  1 用户显式材料 → 2 细纲/合同 → 3 前章末 500 字（必读） → 4 设定 + 同题材拆书事实卡
  → 5 角色状态（必读） → 6 伏笔台账 → 7 滚动章节摘要 → 8 向量/FTS 召回 → 9 可选写法样稿
  按 DEFAULT_INPUT_BUDGET=32000 tokens 自第 9 级向上裁剪；必读集(3,5)保留并记降级事件
  预览：POST /context/preview（展示构成、字符数、Token 估算、是否必读）
```

### 6.7 关键不变式（改动前必读）

1. **事实源单向**：只从 service 落盘；`db.py` 与 `services/__init__.py` 顶部均声明「SQLite 仅索引」。
2. **引擎无副作用**：engine 只返回文本/事件，落盘回 service。
3. **门禁零模型**：门禁不调用 LLM，不落盘。
4. **对话权限实时生效**：执行期每次工具调用前重读会话权限；`discussion_only` 一旦开启立即拒绝写。写域判定与之正交，顺序固定为「只读/仅讨论拒绝（deny）→ 写域 → 批准强度」。
5. **Proposal 是管线唯一出口**；对话写入是 **journal**，两者共享 `file_change_service`。
6. **技能产物只读**：`.dsh/skills/` 只能由 `POST /api/skills/sync` 生成。
7. **路由与写域的事实源是注册表**：Agent 名单、触发词、主责材料、绑定技能来自 `agents/*.yaml`；路由代码不得硬编码这些清单。

---

## 7. 关键业务流程

### 7.1 开书与工作区初始化

入口：书架「新建项目」→ `POST /api/projects`。

1. `project_service` 校验书名/题材/主角/一句话设定；
2. 模板解析（`template_service.resolve_open_template()`）：**显式传入 → `settings.templates.by_genre[题材]` → `by_platform[平台]` → `templates.default` → 内置「默认模板」**；随后按该模板在 `projects/{书名}/` 生成目录骨架与空 md 文件（`备忘录/ 大纲/ 设定/ 状态/ 章节/`，`.meta/` 首次生成任务时创建）；
3. 写 `project.md` 档案；建 `projects` 索引行；刷新文档树。
4. 失败路径：目录已存在 → 报错不覆盖；解析过程异常 → 回退内置「默认模板」。

### 7.2 大纲规划与冻结

入口：`/project/:id/outline`。

`POST /outline/expand`（定向追问）→ `POST /outline/candidates`（每批生成 2–3 个，累计追加）→ 选择候选并反复 `POST /outline/candidates/{candidate_id}/refine`（对话优化）→ `POST /outline/lock`（锁定）→ `PUT /outline` + `POST /outline/freeze`（冻结）。

候选事实源仍是本书 `.meta/outline.json` 的 `candidates`。每项含稳定 `id`、`title`、`content`、`source`；旧项读取时按原位置、标题和正文派生稳定 ID，下次元数据变更时持久化，不删除或改写旧候选。新候选按累计数量分配不重名的标题，锁定信息携带 `candidate_id`。生成模型输入包含此前候选，并要求主线冲突、成长路径和卷级事件具有实质区别；提交对 NFKC / 大小写 / 空白 / 标点等归一化的正文去重，并过滤相似度达到 0.92 的近似重复结果。仅改标题或轻微换词不会持续追加；该文本相似判定不等于语义重复的绝对保证。全重复返回零新增及原因，模型失败或空输出拒绝提交，不用占位替换已有候选。显式 `use_ai=false` 的旧离线手工流程仍可首次创建占位，重复调用不再追加占位。

模型调用在项目锁外执行，提交在项目锁内重读最新元数据、再次去重并合并，避免并发生成丢失候选；追问写元数据也使用同一项目锁。候选优化将原候选全文、继承的 `conversation` 和新 `message` 传入大纲完善任务，成功后追加含 `parent_id`、完整优化正文及用户 / 助手对话记录的新候选。提交前核对源候选版本；模型失败、空内容、归一化后等同于已有候选或源版本冲突时保留原版。优化允许针对同一候选做小幅有效修改，因此仅拒绝归一化相同正文，不套用继续生成的 0.92 近似过滤。对话记录持久化，刷新后选回对应版本可继续查看和优化，正式大纲仍仅在作者锁定后写入。

页面生成按钮在有候选时显示「继续生成」，请求期间显示「正在生成候选…」及带 `role=status` 的等待提示，结束显示本批新增 / 累计 / 重复过滤数量。候选卡标明当前选择和优化来源；独立的「候选对话优化」区展示选定正文与历史，每次发送自动选中新版本。请求期间显示「正在优化候选…」，失败保留修改意见；发送期间新输入也保留供下一轮使用。每个候选的未发送输入独立缓存，切书时清空；页面沿用 `useScopedAction` 防重复提交并拒绝其他项目迟到响应，正式大纲未保存编辑继续受现有保护。

- 冻结状态下修改需带 `confirm`，否则拒绝；页面冻结按钮在已冻结时显示「取消冻结」，调用 `POST /outline/unfreeze` 后恢复「未冻结」状态，可直接保存修改，再按需重新冻结。有未保存修改时，页面提示先保存并阻止重新冻结请求，避免冻结磁盘旧稿；取消冻结仍可在有未保存修改时执行。请求期间显示正在冻结 / 正在取消冻结，使用现有同步提交锁防连点及项目切换后的迟到响应；取消不覆盖页面未保存的大纲输入。
- 冻结与取消冻结均在项目锁内重读合并本书 `.meta/gates.json` 并记录操作日志。取消仅设 `outline_frozen=false`、清空 `outline_frozen_at`，保留其他门禁、正式大纲、候选 / 对话、锁定信息与章节合同；重复取消安全返回未冻结状态。
- `POST /outline/rolling` 只细化近 2 卷，推进后再展开；
- 冻结是大纲可用性的**前置条件**：未冻结时 CHECK 步阻断章节生产。

### 7.3 章节生产 8 步循环

入口：编辑器「8 步循环」→ `POST /api/projects/{id}/pipeline/run`（可 `resume`）。

| 步 | 名称（`STEP_LABELS`） | 做什么 | 落盘 |
|---|---|---|---|
| LOAD | 载入上下文 | `context_service` 组装上下文包 | 任务输入快照（`tasks`） |
| CHECK | 前置依赖门控 | 大纲已冻结 / 合同就绪；文风样本可选，缺失仅提示 | —（必需项缺失才阻断并提示补齐） |
| CONTRACT | 生成/冻结合同 | 情节点清单 + 字数预算 + 章尾钩子 + 涉及实体 | `.meta/contracts.json`、`chapter_contracts` |
| DRAFT | 生成草稿 | 技能、规则与可用认可样稿进入实际生成输入；修订时附上一版候选全文，**只写草稿区** | 候选版本文件 + `proposals`；正文与合同 hash |
| REVIEW | 双门禁 | 硬门与模型逐项审同一份候选正文，空目标也审候选；待核实、AI 失败、伪引用或合同变更不能通过 | `reviews` + 提案 `pipeline_review` 版本凭据 |
| REVISE | 回灌修改 | 带未过项与证据核验原因，使用 `novel.chapter.revise` 生成唯一定位、互不重叠的精确补丁；拒绝整章替换；最多 2 轮，超轮转人工 | 原候选保留，成功修订另存版本 |
| UPDATE | 应用并摄取 | 应用前锁内再查候选/审稿/合同 hash 与文件版本，成功提交才标完成并摄取四件套；冲突明确阻断 | `chapters`、journal、`状态/*.md`、`设定/伏笔管理.md` |
| DECIDE | 状态流转 | 核对本候选是否通过且已应用；未应用只返回待应用，不把旧稿/空章标完成 | checkpoint；质量债按实际情况登记 |

每步落 checkpoint（`.workbench/logs/pipeline/{project_id}/{章节名}.json`），`_first_incomplete()` 决定 `resume` 起点，支持精确步恢复。

检查点协议为 `protocol=2`，保存候选版本链、当前候选与合同 hash。旧协议的待处理提案保留草稿但重跑 REVIEW 及后续；已处理的旧提案明确阻断并提示从新候选开始，不补造旧版审稿凭据。生成失败、补丁格式/定位失败不覆盖原稿；生成与审稿传递取消守卫。UPDATE 遇到已应用提案可依据当前正文摘要恢复提交后、检查点前的崩溃，但摘要不符时拒绝恢复。

DECIDE 对已经应用的候选再次核对当前正文与合同摘要；完成检查点之后作者改稿也不能复用原完成结论，不会覆盖作者新稿。

AI 审稿不可用或审稿中合同改变时直接转人工并保留候选，不能把基础设施失败当成正文质量问题继续修稿。精确证据仅证明模型引用真实存在，是否确实支持该合同项仍依赖模型和作者判断。

AI 不可用后的默认续跑重置 REVIEW/REVISE，重新审保留的同一候选，合同或目标正文 hash 不符仍拒绝续用。合同短编辑的读取、写回与索引同步全程持按书锁；合同生成保持模型调用锁外，提交锁内重读、合并当前章并保护其他章的并发更新，同章期间变更则拒绝覆盖。

### 7.4 审稿与质量债

入口：`/project/:id/review` → `POST /api/projects/{id}/review`。

1. 硬门禁：`density_gate.run()` + `language_gate.run()` + `prose_gate.run()` + `novel_format.run()` + 字数门（下限取本书区间，默认 `MIN_CHAPTER_WORDS=2000`）；超出区间上限只产出非阻断的「字数上限」告警项；
2. 逐项合同验收：关键事件逐项标「已完成 / 未完成 / **待核实**」，**待核实一律不算通过**，并要求正文证据引用；
3. 三层一致性：数值算术 / 设定口径 / 细节连续；
4. 结果写 `reviews` + `quality_debts`（债-1 放行 / 债-2 放行并标记待校订 / 阻断停链），并更新质检矩阵。
5. 表达建议：确定性重复诊断与有证据的模型意见独立呈现，位置按正文（不含旧 frontmatter）；不改变通过条件，不自动修稿。
6. 软审：`POST /review/deslop` → 校验原文、行号、替换与全文版本后，有效行级修改建议进收件箱；无效建议排除，源变更不建提案，应用时仍校验精确原文与基准 hash。界面明确区分模型失败、正常空建议、过滤建议与过期源版本。
7. **只报告，不自动改稿**。审稿页与收件箱复用 `ProseQualityReport` 展示「表达建议（供作者判断）」；缺可选字段的历史结果安全隐藏，编辑候选后提示表达意见对应修改前版本。

### 7.5 章节摄取（追踪四件套）

入口：审稿页「提取四件套」模式的开始按钮或 8 步循环 UPDATE 步 → `POST /projects/{id}/ingest`。四件套是时间线、角色状态、伏笔台账和资源账本，用来承接时间、人物、线索及资源连续性。

流程：在书锁内核验规范 TXT 章节为完成/发表并声明当前版本 → 锁外调用可选结构化提取 → 核验条目结构、有限增减值和逐字原文依据 → 合并 AI 与规则识别的同一原文事件（正文不同位置保留独立事件）→ 锁内再核验源资格与版本并生成完整提交计划 → 逐文件调用现有变更日志 → 专用摘要写入 → 一次数据库事务发布提交清单及角色状态提案。草稿摄取明确拒绝，不写正式追踪文件；提取期间撤回草稿同样拒绝提交。角色状态提案应用和重提也核验源章仍已定稿；作者此前已确认应用的角色状态保留为正式作者材料。

核验后的时间线、资源变化、伏笔埋设自动写入本章托管区域；改稿只替换该章系统记录，块外人工材料保留，块内人工编辑冲突时拒绝覆盖。伏笔已确认的状态和计划回收信息继续保留。账本列为「物品／章节／前值／增减／结余／依据」，无可靠前值及结余时显示待核实，算术检查不会把未知当成通过。角色变化合并成一份完整 `状态/角色状态.md` 候选，保留其他角色、字段及格式；应用前同时校验源章节与目标状态文件版本。旧摄取片段提案用持久基准生成完整差异，不能可靠定位则拒绝应用。

收件箱「按当前状态重提」只针对有持久源版本及结构化变化的状态提案，在书锁内重核来源正文，用当前目标文档生成新完整候选，事务内替换旧候选；当前材料已有同样变化时直接结束旧候选。正文变更则拒绝沿用旧依据，要求重新提取。此操作不调用模型、不触碰自动追踪文件，四件套重放继续提供仍待核对的新候选入口。

重提不能静默恢复原始模型值：候选有未保存编辑时前端阻止请求，已保存人工编辑且与原始字段合成全文不同的候选由后端拒绝重提，保留原候选。首次接管历史条目要求章节、逐字依据和事件各自唯一，且行文本为旧系统精确格式；无法可靠接管的表格、无依据账本或重复条目原样保留并报告可能重复。托管伏笔的依据扩展时，可唯一对齐的同事件沿用回收状态和计划；对齐不明确报冲突。新正文不再提及的已确认记录/回收计划保留并提示核对。

同一当前版本重放返回已提交结果，不重复调用模型或写盘；并发当前章返回 409 忙碌，改稿后重新提取。提交中断时先对照现有变更日志和摘要前后像：写入全部完成且源仍有效则发布原结果，否则仅回退自身可确认的修改；人工后续修改保留并标冲突。摘要只允许 `.meta/summaries.json` 这个内部固定路径，拒绝链接，不扩大普通工具 `.meta` 写域。人物出场只返回名单，不再推断为知道整章摘要；保留已有人工记忆及滚动摘要。尚无可靠归属清单的历史追踪内容保留，不自动删除。

设定总览使用 `material_parser` 统一解析实际标题、属性表、对象表、时间线表与伏笔列表，人物说明与属性不再独立计数。来源顺序为作者显式标注 → 材料来源 → unknown；`ref` 继续作为标注 ID，`rel_path` 才用于原材料跳转。计划伏笔和已埋设伏笔分别统计。伏笔确认在书锁内检查 `expected_hash` 和当前行，仅改状态/计划字段，重复确认幂等。卡片字段编辑新增 `field_edits/expected_hash`，字段删改定位实际列表或属性表；共享表头/自由散文不能独立改名或删除时只禁用对应操作并提供原材料入口，旧 `fields` 继续表示局部更新。

### 7.6 对话修改书稿（三档权限 + 写域两维）

1. 前端选会话/项目/权限模式，可选附加参考文件与指定正文目标章节；
2. `create_run` → 路由（§3.13 两段式）→ 组装上下文（含「本书每章正文目标 X–Y 汉字」）→ 原生 dsh 会话执行；多步计划时按步串行接力；
3. 写工具调用 → `file_change_service`：路径边界 → 版本校验 → 章节门禁（区间下限阻断、上限告警）→ **权限判定（顺序固定）**：
   - **① 只读 / 仅讨论**：`read_only` 或 `discussion_only` 一律拒绝（deny 优先），不受写域与档位影响；
   - **② 写域**：`scope_guard.evaluate()` 判定目标是否在本轮 `write_targets` 内。写 `备忘录/`（自由便签区）永不判越界；
     - **范围内** → 沿用该档位原有语义（**不收紧**）：`ask` 逐次弹卡、`auto` 新建/改写自动且删除/移动弹卡、`full` 自动；
     - **范围外** → `ask`/`auto` 降级为批准卡（`payload.scope` 带越界材料、理由与「仅此次（`once`）/本任务内允许这类材料（`material`）」）；`full` 默认放行但**强制留痕**（运行事件 + 操作日志 + 结果标记），`chat.scope_limit_full=true` 时改为请求批准；`chat.scope_strictness="reject"` 时**直接拒绝**（不弹卡，`code="out_of_scope"`，拒绝原因回灌模型）；
   - **③ 批准强度**：`ask` 除任务级批准外一律出批准卡（含真实差异）；`auto`（默认）仅删除、移动仍需批准；`full` 本书内自动；
   - 章节候选正文由 `_maybe_deslop_chapter()` 先自动去味重写（`auto_deslop`），重写文本即被预览/批准/落盘的那一份；
   - 权限在**每次工具调用前**重读会话，运行中改模式或开「仅讨论」对当前任务立即生效；
4. 批准卡绑定内容与文件版本，版本变更使旧批准失效；重复回复不重复写入；批准卡可选「本任务内不再询问」（仅本 run 内有效）。**越界授权是另一个独立开关**：「本任务内允许这类材料」写入 run 级授权（仅存进程内、run 结束即失效），且**不被**「本任务内不再询问」自动免询；越界授权、放行、拒绝均写入运行事件与操作日志；
5. 完成后可在前端查看差异并 `revert`；「继续处理」在同一会话开新一轮，带入原始要求与已应用变更清单，**先核对磁盘现状**再处理剩余工作。

### 7.7 拆书对标

入口：`/project/:id/teardown`。

导入目标文本（`POST /teardown/import`）→ `analyze`：确定性统计（`analyze_deterministic`：切章、字数、结构）+ AI 六维分析 → 双时间线 + 事实卡（**强制章节依据**，取不到依据标「依据缺失」且不补写）→ 落 `projects/{书名}/拆书/{目标书名}/` → `recall_for_genre()` 按题材召回，注入生成上下文第 4 级。

### 7.8 生图工坊

入口：`/images`（全局，不依赖项目）。

配置图片供应商（`/api/images/providers`）→ 可选上传参考图（`PUT /uploads`，≤5MB）→ `POST /generate`（有 `input_upload_ids` 走垫图/编辑）→ 任务轮询 `/jobs` → 产物落 `.workbench/images/`（图片 + sidecar JSON）→ 画廊 `/records`（收藏、改提示词、删除、下载）。`POST /reindex` 可扫盘重建画廊索引。

尺寸：前端按「基准分辨率 × 比例」计算并与后端 `image_adapters.calc_size()` 对齐。

### 7.9 模型供应商配置与 dsh 投影

1. 设置页填 `baseURL` + API Key → 拉取模型列表 → 勾选批量添加为同一供应商的多个模型（可分别设上下文窗口/最大输出）→ 设全局默认模型 → 健康检查 → 启用；
2. Key 存 `.workbench/secrets.json`（DPAPI 加密），`providers` 表只存 `secret_ref`；
3. 保存/启用自动 `project_to_dsh_home()` 投影到 `.workbench/dsh-home`；「投影到内置 dsh」保留为手动修复入口；
4. 模型选择：任务级（`--patch`）> 项目级（`.meta/engine.json`）> 全局默认；**不得**在 `settings.yaml` 固定 `agent-default-model`。

### 7.10 发布导出

入口：书架「发布导出」。

`GET /publish/platforms` 取平台档案 → `POST /projects/{id}/publish/export`：按平台整理（标题格式、段落缩进、章节分隔、单章建议字数）→ 生成自检清单（字数区间、章节状态、未解决质量债、简介与题材、无凭据泄漏）→ 落 `.workbench/exports/`。

---

### 7.11 Markdown、手动 AI 卡片与正文显示

Markdown 文档由 `MarkdownView` 使用标准 GFM 语法渲染，表格保留对齐、完整细边框、表头底色与单元格换行，宽表仅在独立容器滚动。文档采用中文系统无衬线字体、16px、1.75 行高与最大 960px 宽度；亮、暗、纸主题共用 token。HTML 不执行，只有精确 `<br>` 转成安全换行。仅创作材料中明确的来源字段、来源列或待定占位转换轻量中文标签，保留说明及确认状态，普通英文与代码保持原样。章节 `.txt` 不解释 Markdown。

卡片事实源仍是 `设定/**/*.md`。列表及颜色接口只读本书 `.meta/asset_cards.json`；旧格式解析缓存因 `CACHE_VERSION=2` 失效，不展示「姓名/身份/项目」等旧错误对象。保存后内容 hash 改变即显示待更新；作者点击「AI解析」才启动线程池任务，未变文件按指纹复用，强制解析忽略旧指纹。指纹包含原文、实际模型配置、提示词及 `grounded-cards-v2` 解析协议。结构化输出仅提取已有对象、明确别名、逐字字段、摘要与来源依据；后端重算行号/hash，验证摘句及对象归属，未知来源保留 `unknown`。失败无机械解析回退；写缓存前持项目锁复核文件版本及任务有效性。任务状态落入本书元数据，应用启动恢复中断任务，关闭取消运行。智能生成新设定仍通过提案应用到原文。

对象注册表将颜色绑定 `entity_id`，精确名称或作者显式改名保持身份，不能确认连续性时分配新 ID。跨分类共用确定性色序，分配避开现有及保留颜色，三主题色值持久化；手动色不被重新解析改写，`off` 不自动恢复，恢复自动色会校验冲突。正文按准确名称及明确别名匹配，长名优先，歧义词不染色。颜色、对话斜体、字数节点都不进入事实源。

章节编辑态使用锁定版本的 CodeMirror 6（state 6.7.6、view 6.43.13、commands 6.11.1），提供行号、软换行、撤销、原文定位与精确选区替换。闭合中文引号仅在同一原文段内产生对话斜体。组合输入期间只映射装饰并暂停自动保存/幽灵候选，结束后刷新；控制值的相同回显不产生事务。局部改写绑定生成时的项目、文件、全文及范围，应用前校验；冲突采用及回滚显式更新状态并丢弃旧稿撤销历史，等待期间的新输入保留。文件与模式切换缓存本文件历史及实际父滚动位置。

幽灵续写候选按正文末尾 400 字生成，绑定生成时的项目、章节路径及完整正文。点击「Tab 接受」会在核验来源后直接追加至章末，不要求当前光标在章末，也不替换已有选区；键盘 Tab 仍只在章末空选区时接受，Esc 拒绝不写正文。`ChapterEditor.appendText()` 再核验 CodeMirror 的实时全文、阻止组合输入期间接受，通过一次可撤销的输入事务追加、把光标移到新章末、滚动并恢复编辑焦点；同一候选的重复点击因全文版本变化不能重复追加。编辑、切章、切书、切换显示模式、关闭幽灵候选或进入组合输入时清除候选；迟到生成响应必须通过存活状态、项目、路径和全文比对才能展示。该操作只更新作者编辑草稿，保存继续走原有冲突检测。

`buildNovelDecorations()` 是编辑、预览、阅读共用的位置计算。节点读取 `milestone.enabled/step` 与 `editor.milestone_inline`，按当前原文去空白后的 UTF-16 字符数计算阶段，与汉字门禁分开。跨过阈值后在当前物理段落结束后的下一显示行左对齐显示「已满 N 字」；长段跨过多个档位只显示最新阶段，底部总字数仍实时精确统计。编辑态使用 block widget，预览/阅读使用块级显示装饰。节点 DOM 为空、伪元素显示标签，不占文档字符；保存、复制、AI 上下文和门禁均取纯正文。

## 8. 开发与验证

### 8.1 环境与启动（命令事实）

```powershell
# 安装（首次）
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r workbench/backend/requirements.txt
npm --prefix workbench/frontend install
npm --prefix workbench/frontend run build
.\.venv\Scripts\python.exe workbench\cli\setup_dsh.py

# 启动
.\.venv\Scripts\python.exe -m workbench.backend.app          # http://127.0.0.1:8790
npm --prefix workbench/frontend run dev                      # 开发态热更新（5173 → 代理 /api）
```

### 8.2 测试与静态检查

幽灵续写浏览器回归模块为 `workbench/frontend/tests/ghostText.browser.acceptance.cjs`。先构建 production dist，再由浏览器自动化调用导出的 `runGhostTextAcceptance(page)`，可直接传入 Tabbit 的测试页；模块自身不启动浏览器或服务。覆盖章首/中间选区点击追加章末、焦点/光标与一次撤销、Tab/Esc、连续点击、组合输入，以及改文/切章后的候选与迟到响应失效。所有 API 与静态资产在独立测试页拦截，未知请求阻断；不读取真实小说、不调用模型、不接入 `npm test`。输入法组合使用浏览器 composition 事件验证，未将该检查宣称为操作系统输入法窗口验收。

本专项实测（2026-10-04）：前端现有单测 **124 passed / 0 failed**，TypeScript / Vite 构建与原生对话框静态守卫通过；Tabbit production dist + 合成 API 浏览器回归 **11 passed / 0 failed**，0 页面错误、0 未知 API / 外部网络请求。报告为 `.workbench/logs/ghost-text/browser-acceptance.json`。本次只修改前端接受流程，未重跑全量后端；浏览器功能断言不包含截图捕获或操作系统输入法窗口。

2026-10-04 功能修复实测：后端全量 **1002 passed / 0 failed / 1 skipped**；最终追踪、管线与提案补修联合 **52 passed**，两次范围分开记录。前端 **123 passed**，TypeScript / Vite 构建通过；全站浏览器 **295 passed**，最后改动另以修复专项 **22 passed** 复验。实际迁移和一次真实审稿（供应商 503）、一次真实软审（有效空建议）结果见 [验收记录](工作台功能修复与数据一致性-验收记录-2026-10-04.md)。历史数字保留为各自快照，不累计为当前用例数。

同日大纲候选专项：`test_outline_candidates.py` 与 `test_content.py` 联合 **26 passed**；前端现有测试 **123 passed**，TypeScript / Vite 构建通过。Tabbit 生产 dist + 合成 API 浏览器验收 **9 passed / 0 failed**，覆盖生成等待与防连点、累计追加与重复提示、全重复 / 故障保留、连续优化、候选草稿 / 历史、窄屏、切书迟到响应与 ID 锁定，0 页面错误及 0 未知网络请求。详见 [专项验收记录](大纲候选追加与对话优化-验收记录-2026-10-04.md)；未重跑全量后端，不将模拟模型结果当作真实文学质量结论。

同日冻结切换补修：冻结 / 候选 / 内容三模块联合 **34 passed**，管线针对大纲前置检查 **2 passed / 10 deselected**；现有前端 **123 passed**，最终未保存稿保护改动后再构建通过。最终 production dist 的 Tabbit 专项 **4 passed / 0 failed**，包括冻结 / 取消的等待与防连点、未保存修改保留及阻止冻结旧稿、保存后重新冻结、取消失败保留与切书迟到响应。报告为 `.workbench/logs/outline-freeze/browser/report.json`（31 个合成 API 请求、5 张截图、0 页面错误 / 未知网络）。本地空闲后已重启加载取消接口，未对真实书自动取消冻结，详见同一验收记录的补修节。

```powershell
.\.venv\Scripts\python.exe -m pytest workbench/backend/tests -q      # 后端应用测试
.\.venv\Scripts\python.exe -m pytest workbench/backend/tests/test_outline_candidates.py workbench/backend/tests/test_outline_freeze.py workbench/backend/tests/test_content.py -q # 大纲候选与冻结切换专项
npm --prefix workbench/frontend test                                 # 前端测试（node --test）
npm --prefix workbench/frontend run build                            # tsc + vite build
node workbench/frontend/tests/workbenchRepairs.browser.acceptance.cjs # 生产 dist + 隔离合成 API 的修复验收
python evals/prose-quality/run_eval.py --dry-run                      # 20 原创中文短场景离线字段校验
python evals/prose-quality/check_eval.py                              # 盲评工具标准库自检（不接 pytest / CI）
.\.venv\Scripts\python.exe workbench\cli\check_no_bare_dsh.py        # 裸调 dsh 静态扫描
.\.venv\Scripts\python.exe workbench\cli\check_ui_style.py           # UI 风格 / token 一致性
.\.venv\Scripts\python.exe workbench\cli\check_license_compliance.py # 第三方合规扫描
```

本次复核实测（2026-09-24 复跑）：后端 `pytest workbench/backend/tests -q` → **375 passed，2 warnings，230.11s**；前端 `npm --prefix workbench/frontend test` → **20 passed，0 failed**；`npm --prefix workbench/frontend run build` → 通过（tsc + vite build）。数字随代码变化，引用历史数字时必须标注快照时间。

中文文学质量独立评测入口见 [evals/prose-quality/README.md](../evals/prose-quality/README.md)：原创事实卡与人物知识边界、固定 seed 的场景/AB 盲化、私有方案映射、人工 A/B/tie/both_bad 裁决、读感等维度、事实错误、修改量和成本。`--dry-run` 只校验 20 场景，`check_eval.py` 只验证工具逻辑。胜率仅用明确 A/B 偏好并带 Wilson 95% 区间，未评/平局/两者都不好/未知成本分开报告；不读取项目小说、不调用模型、不自动产生质量结论。真实评测需显式提供通过工作台生成的两方案正文及实际引擎/模型/提示版本/有效参数/预算。

小说 AI 味第一轮优化专项（2026-10-03）：候选审稿、精确修订、样稿来源与真实注入、收件箱复审、取消/并发/恢复和自动去味重放均以临时数据库及假模型回归；章节门禁阈值未调整。最终前端 `npm test` **104 passed**，TypeScript + Vite 构建通过；文学质量工具 20 场景离线校验及 **15 条**标准库自检通过。首次后端全量 **830 passed / 1 failed / 1 skipped**（1036.96s），唯一失败是旧批量流程测试替身把正文当审稿 JSON 返回；仅修正该测试的任务响应并加强候选摘要、逐项证据及最终正文断言后，`test_orchestration.py`、`test_pipeline.py`、`test_pipeline_candidates.py` 联合复跑 **75 passed / 0 failed**（73.41s）。生产验收未放宽，未把局部复跑记作第二次全量通过；原全量日志保留在 `.workbench/logs/prose-optimization/backend-2026-10-03.log`。裸调 DSH 与许可证静态扫描通过；UI 风格扫描仍报告既有 `styles/dashboard.css` 两处 `border-radius: inherit`，该文件未在本专项修改。只读 Tabbit 冒烟遇 `127.0.0.1:8790` 连接拒绝，旧标签页不足以验收新界面，未将新页面浏览器验收记作通过；真实文学质量与连续多章效果尚无盲评结论。

小说 AI 味第二轮优化专项（2026-10-03）：共享表达指导、非阻断重复诊断、可定位模型表达建议和软审建议证据/版本保护已落地，未调整硬门禁或技能源。相关后端联合回归 **272 passed / 0 failed**（218.96s，1 条既有 Starlette/httpx 弃用警告），范围为 `test_candidate_review`、`test_pipeline_candidates`、`test_auto_deslop`、`test_prose_quality`、`test_soft_deslop_evidence`、`test_vector`、`test_pipeline`、`test_orchestration`、`test_proposal_review_api`、`test_knowledge_integration`、`test_file_changes`、`test_writing_style_api`；不记作全量后端复跑。独立重复诊断 26 项与软审证据/原模块 52 项均包含在联合回归中。最终补齐模型表达建议的 LF / CRLF / CR 原文行列后，候选审稿模块另复跑 **36 passed**（13.72s，与联合回归重叠，不累计为新增 36 项）。前端全量 **111 passed**（新增表达建议组件/兼容性 7 项），最终软审状态改动后 TypeScript + Vite 再构建通过。裸调 DSH 扫描 220 文件、许可证扫描 285 文件及限定改动 `git diff --check` 通过。全部模型请求替身、数据库/正文测试目录隔离，无真实小说写入或付费调用；上述结果不证明中文小说读感、长篇连续性或作者修改量已经改善。

第二轮浏览器专项 `node workbench/frontend/tests/proseQuality.browser.acceptance.cjs` 已实际完成 **7 passed / 0 failed**、9 张截图，页面错误 / 控制台错误 / 未知 API / 未知网络请求均为 0（Chromium 154.0.8037.97）。覆盖审稿页原文与行列/重复位置、表达建议不阻断、缺字段旧报告、软审模型失败/源版本变化/过滤/正常空建议，以及收件箱候选复审和编辑后旧证据提示。直接通过浏览器 route.fulfill 读取本仓库生产 `dist`，全部 API 为内存合成数据、未知请求 abort、禁用 service worker；URL 使用固定 8790 但不监听端口、不连接真实后台。报告及截图位于 `.workbench/logs/prose-optimization/second-round-browser/`；这证明本轮 Chromium 界面行为，不替代真实后端联调、其他浏览器或文学质量评测。

近期复核实测（2026-09-25，对话智能体路由与技能体系升级收口后）：后端 `python -m pytest workbench/backend/tests -q` → **438 passed，1 warning，272.90s（0 failed / 0 deselected）**；前端 `npx tsc --noEmit` → 通过（退出码 0），`npm --prefix workbench/frontend run build` → 通过（tsc + vite build，71 modules）；`python evals/run_cases.py --dry-run` → 退出码 0（16 条用例字段合规）。详见 [对话体验改造-验收记录.md](对话体验改造-验收记录.md)。

模型配置与浏览器提示专项复核（2026-10-03）：前端既有 `npm test` 入口 **99 passed / 0 failed**，生产构建 `npm run build` 通过（TypeScript + Vite）；其中新增 `tests/clipboard.test.mjs` **4 项**覆盖长中文/换行/emoji、复制拒绝或异常后的清理与焦点/选区恢复、无支持时只返回应用内错误，以及对话框内临时复制控件不被焦点约束抢焦点。`workbench/cli/tests/test_no_native_dialogs.py` 独立离线自测 **5 项通过**，覆盖 URL 与注释/行号、多行及方括号全局调用、各类浏览器提示入口、应用内确认和显式文件选择允许、注释示例无误报；入口 `python -m unittest discover -s workbench/cli/tests -p test_no_native_dialogs.py -v`。增强守卫扫描 **66 个源码/入口文件通过**，范围及限制见 §4.6；未修改上面的既有命令或引入新依赖。

仪表盘数据专项复核（2026-10-03）：在前端目录执行 `node --test tests/dashboardState.test.mjs` → **5 passed / 0 failed**；`python -m pytest workbench/backend/tests/test_engine.py -k usage_calendar_window_and_filtered_historical_cost -v` → **4 passed / 0 failed**，使用隔离数据库覆盖 1/7/30/90 天窗口、起点午夜、前一日与未来日期边界、书籍筛选、预算阈值，以及当前单价归零后仍按落库值汇总历史成本。上述记录只证明数据与辅助函数专项，页面布局和交互另以浏览器验收结果为准。
本轮最终复核（2026-10-03）：前端 `npm test` **104 passed / 0 failed**，`npm run build`（TypeScript + Vite）通过；后端 `python -m pytest workbench/backend/tests/test_engine.py -q` **19 passed / 0 failed**，其中含上述 4 个日期窗口用例。浏览器曾使用真实本地数据观察亮色主题初始呈现：四项指标同排、每日趋势补齐 7 列且无整页横向溢出。后续浏览器安全策略拒绝重新访问本地预览，暗色/纸感、窄屏和操作交互未完成实测，不将静态响应式规则或代码复核记作浏览器验收通过。

`workbench/frontend/tests/providers.browser.acceptance.cjs` 为专项手动浏览器验收入口，沿用既有 Playwright 运行时和合成 API fixture，不接入 CI，不读取或保存真实作者供应商配置。覆盖凭据显示/隐藏与不回填、模型卡片与多主题/宽度、拉取/批量添加/保存状态、剪贴板与原生提示入口；实际运行状态和浏览器范围由 [专项验收记录](模型配置与浏览器提示优化-验收记录-2026-10-03.md) 单独记载，脚本存在不作为通过证明。 本轮最终专项实测 20 passed / 0 failed（13 张截图，页面 JS dialog/渲染错误/未知 API 各 0）；全站既有浏览器回归 295 passed / 0 failed，13 页面、三主题和六窗口宽度均通过。结果与最后改动的复核范围见专项记录。

### 8.3 常见改动指引

| 动作 | 步骤 |
|---|---|
| 加接口 | `api/<域>.py` 加路由 → `services/<域>_service.py` 实现 → `src/api/client.ts` 加方法 → `types.ts` 加类型 → 页面调用 |
| 加页面 | `src/pages/` 新建 → `src/App.tsx` 注册路由 → `AppShell.tsx` 的 `PROJECT_SECTIONS`/`GLOBAL_SECTIONS` 加导航 |
| 调门禁阈值 | 改 `gates/*.py` 常量 → 同步 `tests/test_gates.py` 正反例 |
| 加图片模型 | 在 `image_adapters.ADAPTERS` **头部**插入适配器类 + 单测 |
| 加技能 | 新建 `skills/<kebab-name>/SKILL.md` → `POST /api/skills/sync` 投影（禁止直接改 `.dsh/skills/`） |
| 加数据库字段 | `db.py` 建表语句 + `_MIGRATIONS` 补列（保持幂等） |
| 加 SQLite 表 | 加入 `TABLE_NAMES`（`is_ready()` 会校验） |

### 8.4 已知限制与技术债

| # | 项 | 影响 | 现状 |
|---|---|---|---|
| 1 | `config.resolve_port()` 保留 `WORKBENCH_PORT` 覆盖 | 与「端口固定 8790」约定不一致 | 待收紧 |
| 2 | `workbench/cli/selfcheck.py`、`verify_isolation.py`、`final_report.py`、`uninstall.py` 与部分 `workbench/tests/{contract,isolation}` 仍含读取用户全局 `~/.dsh` 的旧流程 | 与「禁止读写」红线冲突 | **改造前不得运行** |
| 3 | headless CLI 不支持 `--resume` | 一次性任务无法续接（对话已改用原生 Agent API） | 见 [compat-matrix.md](compat-matrix.md) |
| 4 | `tests/isolation/` I1/I5 依赖全局目录基线 | 无法直接执行 | 待重写为不访问全局目录 |
| 5 | dsh 为 `0.1.0-rc.8` 预发布 | 协议可能随版本变化 | 版本锁定 + 升级前重跑兼容矩阵 |
| 6 | **路由仍有模型与确定性两条路径**：两条路径均保留依据材料与否定写域，非唯一或带语义歧义时可走 LLM | 未配模型/Key 时按注册表与确定性规则回退；复杂语义效果仍须真实评测 | 属预期行为（评测时须一并记录 `source`）；见 `evals/agent-scope/README.md` |
| 7 | **多步计划逐步隔离是「逐步调用」实现**：每步各自起一次 `runtime().stream()`，步骤间不共享模型上下文 | 步骤间信息靠前序产出材料落盘接力；`read_only_refs` 只作提示，不做强制读校验 | 方案 A，已验收「步骤 1 完成后步骤 2 能读到新内容」；跨步记忆不入模型会话 |
| 8 | **`chief-editor` 兼容保留但不执行**：`chat_run_service` 在会话未钉住 Agent 时把它换成 `writing-assistant` | 作者手工指定 `chief-editor` 且未钉住时不会真的由它执行；仅在钉住（`agent_pinned`）时生效 | 保留作路由咨询；如需真正执行须钉住该 Agent |
| 9 | **`evals/` 不接入 pytest / CI**：`evals/run_cases.py --dry-run` 只做离线字段校验 | 用例期望值不会被自动化守护，路由回归可能静默偏移 | 结论由作者真实评测给出；见 `evals/README.md` |
| 10 | headless 采样参数尚未与供应商完成一致性校验 | 任务记录的 temperature 不能直接当作锁定 DSH 模型的已生效参数 | 本轮沿用锁定 vendor，未改适配器；真实 A/B 应记录实际引擎与有效参数，未知项注明未知 |
| 11 | 旧手工维护且没有摄取凭据的章节摘要 | 能核验完成/发表资格，不能证明对应正文版本 | 暂兼容旧格式；新摄取摘要必须匹配提交清单 hash 与值，建议重新提取旧摘要 |
| 12 | `character_service.memory_digest()` / `character_state_summary()` 尚无生产链路中的直接消费者，前者已有测试调用 | 角色状态原文已由上下文第5级使用，记忆文件可检索；不代表两个摘要 helper 已参与生成 | 本轮只读梳理确认，见[状态功能梳理与知识同步验收](状态功能梳理与知识同步-验收记录-2026-10-04.md) |

---

## 9. 文档维护与更新机制

### 9.1 触发条件（满足任一条即必须更新文档）

| 变更类型 | 必须更新的文档 |
|---|---|
| 新增/删除/重命名页面或路由 | 本文件 §4.2；开发文档 §7.1（信息架构） |
| 新增/修改 HTTP 接口（路径、方法、语义） | 本文件 §5 |
| 新增/删除 service/engine/gate 模块 | 本文件 §2、§3 |
| 改动门禁阈值、禁词表、字数/超时等常量 | 本文件 §2.3、§2.6 与开发文档 §3 摘要 |
| 改动数据落盘位置（事实源）或新增 SQLite 表 | 本文件 §2.4、§3；开发文档 §9 |
| 改动对话权限、批准、恢复语义 | 本文件 §3.5、§7.6；开发文档 §6.8；`docs/对话体验改造-验收记录.md`（新增验收记录） |
| 改技能 / Agent / 工作流 / 规则资产 | 本文件 §3.13；`AGENTS.md` 路由表（若意图入口变化） |
| dsh 版本升级或调用协议变化 | [compat-matrix.md](compat-matrix.md)、开发文档 §6、本文件 §2.5 |
| 引入第三方代码或文本 | [third-party-notices.md](third-party-notices.md) + 开发文档 §13 |
| 测试命令或覆盖范围变化 | 本文件 §8.2、[快速启动.md](快速启动.md) §四、[README.md](../README.md) §6 |

### 9.2 责任人（角色制）

| 角色 | 范围 | 责任 |
|---|---|---|
| **变更提出者**（改代码的人/AI 会话） | 其本次改动涉及的文档 | 同一改动内同步更新文档，不留给「以后补」 |
| **架构维护者** | 本文件、开发文档 §4/§6 | 复核分层与依赖纪律是否被破坏 |
| **合规责任人** | `docs/third-party-notices.md`、开发文档 §13 | 任何外部资产引入的登记与放行 |
| **隔离责任人** | `AGENTS.md`、开发文档 §9.7、本文件 §8.4 | 复核红线（DSH_HOME / vendor dsh / 端口）未被绕过 |

### 9.3 流程

1. **改动前**：按 §0.2 定位到对应文档章节，确认当前描述与代码一致；若不一致，先改文档再改代码（文档即契约）。
2. **改动中**：代码与文档在**同一次改动内**完成；引用历史测试数字必须标注快照日期。
3. **改动后自检**（可执行项）：
   - 相对链接可达（本文档内锚点与 `docs/` 内互链）；
   - 命令可执行（§8.1/§8.2 的命令原样跑通）；
   - 数字真实（测试项数、端口、阈值与代码一致）；
   - 术语一致（对照 §0.3 术语表；不混用「索引/缓存」「事实源/运行态」）。
4. **定期复核**：每个里程碑或每轮集中优化结束时，做一次「文档 ≠ 代码」巡检（§9.1 逐条比对），把偏差记入 §8.4。

### 9.4 文档一致性纪律

- **禁止**把设计意图写成实现事实：本文件只写代码里能核实的行为；未实现项归入 §8.4 或开发文档的里程碑/风险章节。
- **禁止**在多个文档重复同一份清单（如接口清单、表清单）后各自漂移：清单只在**本文件**维护，其他文档只链接。
- **禁止**手工编辑 `.dsh/skills/**` 等同步产物。
- 引用代码位置时写相对路径（如 `workbench/backend/services/chat_run_service.py`），不写行号（行号会漂移）。

---

## 10. PC 客户端（`pc-client/`：独立实例形态）

> 快照日期 2026-10-06。Web 端（`workbench/`）与 PC 客户端是**两个相互独立的产品形态**：本节只描述 `pc-client/` 内可核实的行为；除仓库根 `.gitignore` 追加 `pc-client/data/` 外，Web 端文件零修改。

### 10.1 定位与隔离承诺

- `pc-client/` 为**自包含目录**（backend/frontend/vendor/cli/skills/agents/templates/workflows 全部随目录分发），不引用仓库内 `workbench/` 或仓库根资产即可运行，可整体复制为独立 GitHub 仓库（见 `pc-client/README.md`）。
- 副本导入路径为 `pc_client.backend.*`；物理代码在 `pc-client/backend/`，由 `pc-client/pc_client/__init__.py` 的 `__path__` 扩展 shim 解析（`pc_client/__init__.py` 把包搜索路径扩展到 pc-client 根，`pc_client.backend` 命中 `pc-client/backend/`）。
- **数据物理隔离**：客户端一切可写数据落在数据根（默认 `pc-client/data/`），与 Web 端 `.workbench/`、`projects/` 零共享（不共享 DB 文件/连接/WAL/SHM/目录），两端可并存。
- **无自动同步**：两实例间没有任何自动数据通道；跨端迁移仅经项目包导出/导入（§10.6 的 transfer 事务性补强同样适用于客户端副本）。
- 红线继承：只用 vendor 内置 dsh（`pc-client/vendor/dsh/`）；绝不触碰 `~/.dsh`；端口不碰 {3080, 8765, 3456} 且不占 Web 端 8790。

### 10.2 双根模型（`pc-client/backend/config.py`）

| 根 | 符号 | 取值 | 承载内容 |
|---|---|---|---|
| 代码根 | `APP_ROOT`（`PROJECT_ROOT` 为兼容别名） | config.py 上一级 = pc-client 目录 | `skills/ agents/ templates/ workflows/ rules/`（代码资产）、`frontend/dist`、`cli/`、`vendor/dsh` |
| 数据根 | `DATA_ROOT` | env `WORKBENCH_DATA_DIR` 优先，否则 `APP_ROOT/data`；`set_data_root()` 运行时切换并重算全部派生常量 | 一切可写数据（下表） |

数据根内扁平落点：`workbench.db`（含 WAL/SHM）、`projects/`、`snapshots|exports|logs|models|vector|images|trash|chat|backups/`、`settings.json`、`secrets.json`、`.dsh/skills/`（技能投影）、`dsh-home/`（独立 DSH_HOME，`dsh_home_dir()` 动态取值）、`instance.json`。`--data-dir` 由客户端壳转成 env 实现（§10.7）。

### 10.3 实例模式与隔离自检（`pc-client/backend/services/instance_service.py`）

- `ensure_instance_marker()`：幂等写 `DATA_ROOT/instance.json`（`instance_id` uuid4 hex、`mode: "client"`、`data_root`、`created_at`；临时文件 + 原子替换）。
- `run_isolation_selfcheck()` 五项：数据根≠代码根；数据根不是代码根祖先（拦「数据根设成仓库根」）；DB 不落 Web 运行时（DB 路径 ≠ `pc-client 上级/.workbench/workbench.db`，且「workbench.db+dsh-home+settings.json 齐备而 instance.json 缺失」视为疑似 Web 运行时目录拒绝）；端口不在保留集；marker 可解析。失败汇总为 `IsolationViolationError`（中文逐条明细）。
- 启动集成：`app.py` 的 `main()` 启动前**硬自检**，失败打印中文错误 return 1；`_lifespan` **软自检**结果存 `app.state.client_info`（不抛错，保证 TestClient 场景可用）。

### 10.4 端口策略（`config.py`）

- `RESERVED_PORTS = FORBIDDEN_PORTS ∪ {DEFAULT_PORT(8790), 5173}`。
- `resolve_client_port()`：env `WORKBENCH_PORT` 非空则校验（1-65535 且不在保留集，违反报中文错误）；否则 bind `127.0.0.1:0` 取系统空闲端口。`resolve_port()`（Web 语义）保持不变。

### 10.5 `/api/client` 接口清单

| 方法与路径 | 语义 |
|---|---|
| `GET /api/client/info` | 实例信息：`mode/instance_id/data_root/db_path/port/created_at/selfcheck{ok,checks[]}` |
| `GET /api/client/verify-isolation` | 实时重跑五项自检：`{ok, checked_at, checks[]}` |
| `POST /api/client/open-data-dir` | 打开数据根目录（Windows `os.startfile`，失败 500） |
| `POST /api/client/backup` | 立即备份 → `{name,size_bytes,created_at,path}` |
| `GET /api/client/backups` | 备份列表（`created_at` 倒序） |
| `POST /api/client/restore` | body `{name}` → `{ok,restored_from,previous_archive}` |
| `DELETE /api/client/backups/{name}` | 删除备份 |

### 10.6 备份与恢复（`pc-client/backend/services/client_data_service.py`）

- **backup**：zip 写入 `DATA_ROOT/backups/backup-YYYYMMDD-HHMMSS.zip`（同秒加 `-N`）；白名单内容 `workbench.db*`、`projects/**`、`settings.json`、`secrets.json`、`snapshots/**`、`instance.json`；打包前 `PRAGMA wal_checkpoint(TRUNCATE)`；logs/vector/models/images/exports/chat/trash/dsh-home/.dsh/backups 一律排除（可再生或非数据）。
- **restore**：name 白名单正则（防路径穿越）→ zip-slip 防护解压到 `restore-tmp-*` → `PRAGMA integrity_check` 必须为 ok → 当前数据**移动**留档到 `backups/pre-restore-<ts>/` → 逐条切换 → `db.init_db()` 重初始化 → 清理临时目录；中途异常回滚留档内容并原样抛错，当前数据无损。
- **list/delete**：列表倒序；delete 需 name 匹配模式且存在（否则 404）。

### 10.7 客户端壳（`pc-client/client/main.py`）与打包（`pc-client/build/`）

- 开发态入口 `python client/main.py [--data-dir <路径>] [--port <端口>] [--no-window] [--print-url]`：设 `WORKBENCH_DATA_DIR`（默认 `pc-client/data`）与 `WORKBENCH_PORT`（自动选取空闲端口，禁用 {3080,8765,3456,8790,5173}）后以子进程启动 `pc_client.backend.app`，日志追加 `data/logs/backend.log`；探活 `/api/health` 后开窗。pywebview 为**可选依赖**：未安装或 `--no-window` 时自动降级系统浏览器（功能等价）。退出流程：`CTRL_BREAK_EVENT` → 15s → terminate → kill（含进程树兜底），backend 日志出现 uvicorn `Application shutdown complete` 即优雅收尾。
- 打包（onedir，`build/build.ps1` → `build/dist/AiWriteStudioClient/`）：backend/pc_client shim/frontend/dist/vendor/skills/agents/templates/workflows/cli 以**源码 data** 形式进 `_internal/`（不进 PYZ），hiddenimports 以 `backend.*` 别名收集第三方依赖；冻结分支把 `_internal` 插入 `sys.path[0]`、数据根 `setdefault` 为 **exe 同级 `data/`**（环境变量可显式覆盖）、uvicorn `Server.run()` 跑 daemon 线程 + pywebview 主线程开窗、窗口关闭置 `should_exit` 优雅收尾。产物约 337MB（vendor 占 205MB）。
- 验收快照（2026-10-06）：`--print-url` 输出 `CLIENT_URL=` 且 exit 0；`/api/health` 200（`vendor_dsh:true` 证明 APP_ROOT 解析正确）；数据落 exe 同级 `data/`；宿主 `.workbench/` 文件数/字节/最新 mtime 指纹前后一致。

### 10.8 前端「客户端数据」面板与同步工具

- 前端（`pc-client/frontend`，代理目标 `PC_BACKEND_PORT || "8791"`）：设置页 `/settings` 新增「客户端数据」分区，仅在 `GET /api/client/info` 返回 `mode==="client"` 时渲染（Web 端无此端点自动隐藏）；展示实例信息与自检逐项结果、醒目文案「PC 客户端与 Web 端相互独立，不做自动同步」，提供立即备份/备份列表/恢复（二次确认，成功提示 `previous_archive` 留档路径）/删除/打开数据目录。
- 同步工具 `pc-client/tools/sync-from-workbench.py`（可选）：从 Web 端选择性拉取 backend/frontend/vendor/cli/skills/agents/templates/workflows 更新；`--preview`（默认）/`--yes`/`--only`/`--prune`；对 backend/cli 范围文本文件复制时自动重跑导入路径改写（`workbench.backend`→`pc_client.backend` 等三变体）；node_modules/dist/数据目录排除。

### 10.9 测试落点（pc-client/backend/tests）

| 文件 | 覆盖 |
|---|---|
| `test_data_root.py` | 默认数据根、env 导入期/子进程生效、set_data_root 全落点联动、宿主零写入冒烟、隔离启动冒烟 |
| `test_client_instance.py` | info/verify 契约、伪造 Web 运行时拒绝、祖先目录拒绝、动态端口与保留集、instance.json 幂等、双数据根并行写入互不影响 |
| `test_client_data.py` | 备份-修改-恢复往返、损坏包拒绝且数据无损、zip-slip/非法名、列表倒序与删除、API 契约 |
| `test_transfer_roundtrip.py` | A→B 项目包迁移逐字节一致、源端不变、失败零残留、无静默同步 |

数字快照（2026-10-06，`cd pc-client && python -m pytest backend/tests -q`）：**1130 passed / 1 skipped / 0 failed**；前端 `npm test` 134 passed（`cd pc-client/frontend`）。

---

## 11. 安装包与下载体验

> 快照日期 2026-10-06。本节描述 PC 客户端的分发链路：绿色版目录 → 安装包 → 下载页；均属 `pc-client/` 自包含范围，Web 端零修改。

### 11.1 安装模式数据根回退（`pc-client/client/data_root.py`）

冻结模式下数据根默认值解析为纯决策函数 `resolve_frozen_data_root(exe_dir, env_value, writable_check)`，顺序：

1. env `WORKBENCH_DATA_DIR` 显式指定 → 原样使用（展开 `~` 与环境变量），**永远优先**；
2. exe 同级 `data/` 可写（真实探测：临时目录+探测文件，即用即删）→ 用 exe 同级 `data/`（绿色版行为不变）；
3. exe 同级不可写（安装到 Program Files 的典型情形）→ 回退 `%LOCALAPPDATA%\AiWriteStudioClient\data`，启动时打印中文决策说明。

纯函数幂等、探测函数可注入（`backend/tests/test_installed_data_root.py` 覆盖三种决策与幂等性）。开发态（非冻结）行为完全不变。卸载器只删安装目录，用户数据（LOCALAPPDATA）不受影响。

### 11.2 安装向导（`pc-client/installer/AiWriteStudioClient.iss`，Inno Setup 6）

- 中文向导：欢迎 → 许可协议确认（`LICENSE-INSTALLER.txt`，作者可替换）→ 路径选择（默认 `{autopf}\AiWriteStudioClient`）→ 快捷方式任务（desktopicon/startmenu 默认勾选可取消）→ 安装进度 → 完成页（`FinishedLabel` 覆盖为「安装成功」文案 + postinstall「立即运行」，`skipifsilent`）。
- `AppId` 固定 GUID；`#ifndef MyAppVersion/AppSourceRoot` 允许 ISCC `/D` 传参覆盖；`PrivilegesRequiredOverridesAllowed=commandline`（验收可用 `/CURRENTUSER` 免提权，默认仍 admin）。
- 卸载器自动生成（`unins000.exe`）；未写 `[UninstallDelete]`，卸载只删 `{app}`，`%LOCALAPPDATA%` 用户数据保留。
- 已知构建坑：产物内 vendor node_modules 深路径超 MAX_PATH 会使 ISCC 读文件失败——`build-installer.ps1` 用临时短路径 junction 规避（`C:\asc-build` → 产物目录，失败回退 `%TEMP%`，finally 清理）。
- 构建机依赖 Inno Setup 6（winget `JRSoftware.InnoSetup`，用户级安装）；中文语言文件 `ChineseSimplified.isl` vendor 自 issrc 官方仓库（官方安装包不含 Unofficial 语言）。

### 11.3 全链路构建（`pc-client/build/build-installer.ps1`）

五步编排（每步失败中文报错中止）：①前端 dist 检查 → ②PyInstaller（复用 `build.ps1`，`-SkipPyInstaller` 跳过）→ ③签名钩子（`sign.ps1` 存在则分两阶段 `-Files` 调用并透传 `-SelfTest`，不存在打印「跳过签名」继续）→ ④ISCC 编译（`PC_ISCC` > 常见安装路径 > PATH；`/DMyAppVersion` `/DAppSourceRoot` 传参）→ ⑤产物校验（`build/dist/installer/AiWriteStudioClient-Setup-<版本>.exe` >10MB）。参数：`-SkipPyInstaller`、`-SelfTest`、`-Version`、`-Verify`（内置静默安装→探活→卸载验收）。

### 11.4 数字签名（`pc-client/build/sign.ps1`）

模式决策：env `PC_SIGN_PFX`/`PC_SIGN_PFX_PASSWORD`（或 `-PfxPath`）→ **正式模式**（受信任 CA 证书 + RFC3161 时间戳 `/tr http://timestamp.digicert.com /td SHA256`）；否则 `-SelfTest` → **自签模式**（`New-SelfSignedCertificate` CodeSigning → PFX 导出至 `build/certs/`（gitignore，幂等复用）→ 签名 → 证书导入 CurrentUser 存储（`TrustedPeople` + `certutil -user -addstore Root` 导入根——实证 Authenticode 验签不认 TrustedPeople 对等信任，缺根导入会报 `CRYPT_E_UNTRUSTEDROOT`）→ 验签 Valid）；两者皆无 → 退出码 3 + 醒目告警与 CA 申请指引，**不阻断构建**（build-installer.ps1 对 exit 3 放行）。

签名工具：优先 signtool（`Get-Command` / Windows Kits bin 扫描，`/fd SHA256`），缺失时回退 PowerShell 原生 `Set-AuthenticodeSignature`（`-HashAlgorithm SHA256 -IncludeChain All`）。验签统一 `Get-AuthenticodeSignature` Status=Valid。

**诚实边界**：自签证书仅本机可信（导入信任后本机运行无「未知发布者」告警）；要消除分发对象的 SmartScreen 告警必须使用受信任 CA（OV/EV）代码签名证书。

### 11.5 下载页与后端路由

- **静态页**（`pc-client/download/index.html`，单文件中文零依赖）：下载源解析 `?src=` > `__DOWNLOAD_SOURCE__` 标记（静态托管未替换 = 未配置，仅 `?src=` 可用，显示内联提示）；fetch 流式下载 + Content-Length 定态进度（百分比+MB）/无 CL 不确定态；Blob + `a[download]` 保存；完成自动提示（顶部横幅 + Notification API 权限可用时）+ 四步安装指引 + 三情形签名如实说明卡（自签/CA/未签的 SmartScreen 表现）。
- **后端路由**（`backend/api/download.py`，注册于 SPA catch-all 之前）：`GET /download` 读取 index.html 并把 `__DOWNLOAD_SOURCE__` 替换为 `/download/setup` 后返回（文件缺失 404 中文）；`GET /download/setup` FileResponse 流式（自带 Content-Length）；`GET /download/setup/info` 返回 `{name,size_bytes,mtime,path}`。安装包定位：env `PC_SETUP_FILE` > `build/dist/installer/` 最新 `*-Setup-*.exe` > 404 中文。

### 11.6 测试落点与数字快照

| 文件 | 覆盖 |
|---|---|
| `test_installed_data_root.py` | 冻结数据根三决策（显式 env/可写同级/只读回退）与幂等 |
| `test_download_routes.py` | /download 三端点契约、标记替换、定位顺序、404 中文 |

数字快照（2026-10-06）：`test_installed_data_root` + `test_data_root` 13 passed；`test_download_routes` + `test_client_instance` 31 passed；安装包验收——`/VERYSILENT /CURRENTUSER` 静默安装 exit 0、安装目录启动 `/api/health` 200、卸载 exit 0 且 exe 清理。产物：`AiWriteStudioClient-Setup-0.1.0.exe`（94.7MB）。

---

## 附录 A：受管资产清单

| 目录 | 现状 | 事实源 | 说明 |
|---|---|---|---|
| `skills/` | 8 个技能（各含 7 节契约化 `SKILL.md` + `references/`，共 9 份明细） | 是 | `character-arc`、`foreshadow-check`、`human-linguistics`、`novel-planning`、`novel-review`、`novel-setting`（2 份引用）、`novel-teardown`、`novel-writing`；改后须 `POST /api/skills/sync` |
| `.dsh/skills/` | 8 项技能投影（`workbench-rules` 编译产物按需出现） | 否 | 同步产物（投影 `references/`），**禁止手工编辑**；`workbench-rules` 由 `POST /rules/compile` 生成 |
| `agents/` | 8 个 YAML | 是 | `chief-editor`、`context-keeper`、`planner`、`reviewer`、`setting-keeper`、`teardown-analyst`、`writer`、`writing-assistant`；内置不可删；内置定义带 `version`（当前 3，`teardown-analyst` 为 4），含 `materials`/`capabilities`/`boundaries`；升级时保留作者改过的模型/温度/启停，作者自建同名文件不覆盖 |
| `rules/` | 按需创建 | 是 | 全局规则根；作品级规则在 `projects/{书名}/.meta/rules.json`；编译产物 `.dsh/skills/workbench-rules` |
| `workflows/` | 1 个 | 是 | `默认八步产章.json` |
| `templates/` | 1 个 | 是 | `默认模板`（内置、只读，可复制后修改）；自定义模板可新建/复制/重命名/删除/导入导出，其默认与题材/平台适配存 `.workbench/settings.json` 的 `templates` 段 |

## 附录 B：运行时目录（可删除重建）

| 路径 | 内容 |
|---|---|
| `.workbench/workbench.db` | SQLite 索引（36 个初始化表 + 按需 `workspace_changes/ingestion_runs` + `docs_fts`） |
| `.workbench/vector/books/<knowledge_key>/vectors.db` | 每书独立向量、图谱、证据及任务；旧共用库只保留历史 |
| `.workbench/dsh-home/` | 独立 DSH_HOME（含 `settings.yaml`、sessions） |
| `.workbench/chat/{sessions,runs}/` | 对话会话快照与运行事件、交互记录 |
| `.workbench/logs/pipeline/{project_id}/` | 8 步循环 checkpoint |
| `.workbench/snapshots/`、`trash/` | 快照与回收站 |
| `.workbench/exports/`、`images/` | 导出产物、生图产物（图片 + sidecar JSON） |
| `.workbench/secrets.json`、`settings.json` | 凭据（DPAPI 加密）、工作台偏好 |
| `.workbench/chat-acceptance/` | 浏览器验收用的隔离测试书与设置（仅测试） |

## 附录 C：术语与命名对照

| 中文 | 代码符号 | 备注 |
|---|---|---|
| 请求批准 / 帮我批准 / 完全访问 | `permission_mode`: `ask` / `auto` / `full` | 旧值 `auto_apply`/`mode` 保留兼容映射 |
| 仅讨论 | `discussion_only` | 与权限档位独立 |
| 收件箱 | `proposals`、`Inbox.tsx` | AI 写入类产出的唯一出口 |
| 质检进度表 | `quality_matrix` | 逐章八项，含格式门和硬门禁总状态 |
| 质量债 | `quality_debts` | 债-1 / 债-2 / 阻断 |
| 幽灵文本 | `ghost-text` | 候选不落盘 |
| 章节合同 | `chapter_contracts`、`.meta/contracts.json` | 冻结后有 `confirm` 才能改 |
| 中断 | run status `interrupted` | 进程重启后由 `recover_interrupted()` 标记 |

---

**维护提示**：本文档的任何修改都应先核对代码；若发现本文件与代码不一致，以代码为准并立即回修本文件，同时在 §8.4 记录偏差来源。
