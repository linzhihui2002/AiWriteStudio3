# AGENTS.md — AI 小说创作工作台

本仓库是「AI 小说创作工作台」：本地优先的中文小说创作工具。

- 后端：`workbench/backend/`（Python / FastAPI）
- 前端：`workbench/frontend/`（React + Vite）
- 技能源：`skills/<name>/SKILL.md`（dsh 技能格式，`name` 必须 kebab-case）
- 小说与参考目录：`projects/`（已被 .gitignore 忽略）；工作台登记的小说目录可按作者权限修改，第三方参考目录只读。
- 开发文档：`docs/AI小说创作工作台-开发文档.md`（产品/需求/方案）
- 技术文档：`docs/AI小说创作工作台-项目技术文档.md`（**实现级**：分层、模块、接口清单、数据流、业务流程、文档更新机制）

## 1. 隔离红线（任何会话都必须遵守）

1. 工作台只使用**独立 DSH_HOME** = `.workbench/dsh-home`；
   **禁止读写用户全局 `~/.dsh`**（本机为 `C:\Users\30332\.dsh`）。
2. 只调用 **vendor 内置、版本锁定**的 dsh：`workbench/vendor/dsh/`。
   **禁止**依赖 PATH 上的全局 `dsh`；**禁止** `npm install -g`。
   所有 dsh 调用须经 `workbench/backend/engine/dsh_paths.py`。
3. 端口固定 `8790`；禁用 `3080`（dsh web）、`8765`、`3456`。
4. `projects/` 中第三方参考目录只读；工作台已登记的当前小说目录允许按作者授权读写。
   不得把整个 `projects/` 作为写入根目录，不得跨书写入或通过链接绕过边界。
   禁止把参考项目内容原样复制进 `workbench/` 或 `skills/`；引入任何第三方代码/文本前先查 `docs/third-party-notices.md`。

## 2. 路由表（意图 → 技能 / 执行 Agent）

意图触发词与候选 Agent **来自 `agents/*.yaml` 的能力注册表**（`agent_service.capability_index()`），代码里没有硬编码的意图表；下表是常用入口的速查，**实际执行角色由两段式路由决定**（关键词快路径 → LLM 语义判定 → 确定性回退），并据此授予「本轮写域」。

| 意图 | 触发词（用户可能怎么说） | 建议执行角色 | 技能 / 落点 |
|---|---|---|---|
| 章节审稿 | 审稿、审这章、验收、挑毛病、这章行不行 | `reviewer`（**只读**，不改稿） | `skills/novel-review/SKILL.md` |
| 去 AI 味 | 去AI味、去味、太AI了、没人味、像AI写的 | `reviewer`（**只读**） | `skills/human-linguistics/SKILL.md` |
| 大纲规划 | 列大纲、细纲、卷纲、章节规划、接下来写什么 | `planner`（写 `大纲/`） | `skills/novel-planning/SKILL.md` |
| 章节写作 | 写这一章、续写、写正文、扩写这一节 | `writer`（写 `章节/`） | `skills/novel-writing/SKILL.md` + `human-linguistics` |
| 设定管理 | 设定、人物卡、世界观、设定卡 | `setting-keeper`（写 `设定/`） | `skills/novel-setting/SKILL.md` |
| 状态 / 连续性 | 角色状态、时间线、资源账本、追踪四件套 | `context-keeper`（写 `状态/`、`设定/`） | `novel-setting` + `character-arc` |
| 拆书对标 | 拆书、拆解这本书、对标、爆款分析 | `teardown-analyst`（**只读本书**，技能 `novel-teardown`） | `skills/novel-teardown/SKILL.md` + 拆书资产库（项目页 `/teardown`，服务 `workbench/backend/services/teardown_service.py`；六维**自定**、事实卡强制章节依据、按题材召回注入上下文第 4 级） |
| 伏笔检查 | 伏笔、埋的线、回收、坑没填 | `context-keeper`（写 `状态/`、`设定/`） | `skills/foreshadow-check/SKILL.md` |
| 角色成长检查 | 角色成长、人物弧光、OOC、人设崩了 | `context-keeper` | `skills/character-arc/SKILL.md` |
| 兜底 / 未命中 | 以上都不像、杂项要求 | `writing-assistant`（默认执行者，先声明本轮动哪份材料） | 仅 `human-linguistics` |
| 合规 / 许可证 | 引入第三方、许可证、GPL、MIT、合规扫描 | — | `docs/third-party-notices.md`、`workbench/cli/check_license_compliance.py` |
| 门禁脚本 | 字数门、语言门、禁词门、密度检查、格式门、账本校验 | — | `workbench/backend/gates/`（章节硬门禁 4 个 + 追踪/结构校验 3 个） |
| 规则编译产物 | 写作规则、本书规则、规则冲突 | — | `.dsh/skills/workbench-rules/SKILL.md`（自动生成，勿手工编辑） |

**只读角色**：`reviewer`、`teardown-analyst`、`chief-editor` 与兜底 `writing-assistant` 的写域恒为空，不会写书稿；`chief-editor` 仅作路由咨询（会话未钉住它时会被换回 `writing-assistant`，见技术文档 §8.4）。

**技能同步**：`skills/` 是源目录（含 `references/`），改动后跑
`POST /api/skills/sync`（或工作台「设置 → 技能 → 同步」）投影到 `.dsh/skills/`；
**禁止手工编辑** `.dsh/skills/` 下的产物。

**评测集**：`evals/agent-scope/cases.json`（16 条路由与写域用例，含越界反例）由 `evals/run_cases.py` 驱动；`--dry-run` 只做离线字段校验，**不接入 pytest / CI**。详见 `evals/README.md`。

## 3. 硬门禁摘要（一句话级）

- **字数门**：单章正文汉字数 ≥ 本书字数区间下限（默认 2000）；不足即拒收，高于上限仅告警。
- **语言门**：正文必须为中文散文；HTML 标签 / 注释 / 实体一律阻断；
  外语 token 须经用户单独确认后在 `.deslop-whitelist` **精确登记**方可保留。
- **禁词门**：破折号 ≤ 每 250 字 1 个；对话引号 ≥ 每 120 字 1 次；
  禁词表零命中（含「突然 / 忽然 / 仿佛 / 只见 / 缓缓 …」）。
- **去 AI 味门**：三连排比（不是A，不是B，不是C）全禁；叙述层推测词零容忍。
- **格式门**：正文禁止 Markdown 标记泄漏（`#` 标题行、`**` 强调、列表、引用、
  代码围栏、`---` 分隔线、行内代码 / 链接、HTML 标签与实体）与重复章节标题行；
  成对引号须配对，半角标点（`...` / `--` / 汉字后半角逗号句号）一律阻断。
- **审稿门**：逐项验收，**待核实一律不算通过**；只报告，不自动改稿。
- **追踪 / 结构校验**（只报告不改写）：账本「前值 + 变动 = 结余」与条目 ID 唯一性、
  章纲章号唯一 / 连续与卷末钩子对应、设定卡必填字段与来源分级（`tracking_gate` / `outline_gate` / `setting_gate`，
  由对话工具 `check_tracking` 调用，**不参与**章节硬门禁）。

**章节存储**：章节正文为 `章节/第NNNN章.txt`，文件内**只有纯正文**（无 frontmatter、无标题行）；
标题 / 状态 / 合同ID / 字数以库表 `chapters` 为准；存量 `第NNNN章.md` 由迁移任务幂等转成 `.txt`。

**对话审批**：默认自动执行（`auto`），**范围内**仅删除 / 移动这类破坏性操作弹卡；`ask` 是可选严格模式，
支持「本任务内不再询问」（仅本 run 内有效）；切换权限**无需新开对话**，当前任务的下一个工具调用即生效。

**写域（本轮能改哪些材料）**：路由给每轮授予写域（如「完善设定」只写 `设定/`），与批准强度是**两个正交维度**，
判定顺序固定为 **只读 / 仅讨论拒绝（deny）→ 写域 → 批准强度**：

- **范围外写入在 `auto` 档也会弹卡**（与删除 / 移动同形态的批准卡，标注越界范围与目标材料）；
- 越界批准卡给两个粒度：「仅此次批准」与「**本任务内允许这类材料**」——它与「本任务内不再询问」是**两个独立开关**，互不扩权；
- `full` 档越界默认放行但**强制留痕**；设为「完全访问也受写域限制」后改为请求批准；严格模式（`scope_strictness=reject`）下**范围外一律拒绝**；
- **`备忘录/` 是自由便签区**：写它永不判越界；只读 / 仅讨论状态下任何写入一律拒绝，不受档位与写域影响。

门禁实现与阈值唯一事实源：`workbench/backend/gates/`
（章节硬门禁 `density_gate` / `prose_gate` / `language_gate` / `novel_format`；
追踪 / 结构校验 `tracking_gate` / `outline_gate` / `setting_gate`）。
自检：`python -m pytest workbench/backend/tests -v`。

## 4. 详细规则（渐进披露）

本文件只放路由与门禁摘要，详细规则一律下沉：

- `skills/<name>/SKILL.md` — 技能正文、验收清单、改写手法
- `docs/AI小说创作工作台-开发文档.md` — 产品 / 架构 / 需求
- `docs/AI小说创作工作台-项目技术文档.md` — 实现级架构、模块与接口清单、数据流、业务流程；
  **改代码时须在同一改动内同步更新该文档**（触发条件与流程见其 §9）
- `docs/third-party-notices.md` — 第三方合规登记（引入前必查）
- `workbench/backend/gates/*.py` — 门禁阈值与词表
- `workbench/cli/check_license_compliance.py` — 禁用项目特征扫描
- `evals/README.md` — 路由与写域评测集说明（`--dry-run` 离线校验，**不接入 pytest / CI**）

## 5. 技能同步

`skills/` 是**源目录**；同步器会把技能投影到 `.dsh/skills/`
（dsh 项目级技能根，发现 rank 100，格式 `<name>/SKILL.md`）。
同步器已实现：修改源目录后执行 `POST /api/skills/sync`，或在工作台“设置 → 技能”中点击“同步”。**禁止**手工编辑 `.dsh/skills/` 下的产物。
