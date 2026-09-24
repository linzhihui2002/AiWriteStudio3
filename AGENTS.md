# AGENTS.md — AI 小说创作工作台

本仓库是「AI 小说创作工作台」：本地优先的中文小说创作工具。

- 后端：`workbench/backend/`（Python / FastAPI）
- 前端：`workbench/frontend/`（React + Vite）
- 技能源：`skills/<name>/SKILL.md`（dsh 技能格式，`name` 必须 kebab-case）
- 小说与参考目录：`projects/`（已被 .gitignore 忽略）；工作台登记的小说目录可按作者权限修改，第三方参考目录只读。
- 开发文档：`docs/AI小说创作工作台-开发文档.md`

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

## 2. 路由表（意图 → 技能）

| 意图 | 触发词（用户可能怎么说） | 路由目标 |
|---|---|---|
| 章节审稿 | 审稿、审这章、验收、挑毛病、这章行不行 | `skills/novel-review/SKILL.md` |
| 去 AI 味 | 去AI味、去味、太AI了、没人味、像AI写的 | `skills/human-linguistics/SKILL.md` |
| 大纲规划 | 列大纲、细纲、卷纲、章节规划、接下来写什么 | `skills/novel-planning/SKILL.md` |
| 章节写作 | 写这一章、续写、写正文、扩写这一节 | `skills/novel-writing/SKILL.md` |
| 设定管理 | 设定、人物卡、世界观、角色状态、时间线 | `skills/novel-setting/SKILL.md` |
| 拆书对标 | 拆书、拆解这本书、对标、爆款分析 | 拆书资产库（项目页 `/teardown`，服务 `workbench/backend/services/teardown_service.py`；六维**自定**、事实卡强制章节依据、按题材召回注入上下文第 4 级） |
| 伏笔检查 | 伏笔、埋的线、回收、坑没填 | `skills/foreshadow-check/SKILL.md` |
| 角色成长检查 | 角色成长、人物弧光、OOC、人设崩了 | `skills/character-arc/SKILL.md` |
| 合规 / 许可证 | 引入第三方、许可证、GPL、MIT、合规扫描 | `docs/third-party-notices.md`、`workbench/cli/check_license_compliance.py` |
| 门禁脚本 | 字数门、语言门、禁词门、密度检查 | `workbench/backend/gates/` |
| 规则编译产物 | 写作规则、本书规则、规则冲突 | `.dsh/skills/workbench-rules/SKILL.md`（自动生成，勿手工编辑） |

**技能同步**：`skills/` 是源目录，改动后跑
`POST /api/skills/sync`（或工作台「设置 → 技能 → 同步」）投影到 `.dsh/skills/`；
**禁止手工编辑** `.dsh/skills/` 下的产物。

## 3. 硬门禁摘要（一句话级）

- **字数门**：单章正文汉字数 ≥ 2000；不足即拒收。
- **语言门**：正文必须为中文散文；HTML 标签 / 注释 / 实体一律阻断；
  外语 token 须经用户单独确认后在 `.deslop-whitelist` **精确登记**方可保留。
- **禁词门**：破折号 ≤ 每 250 字 1 个；对话引号 ≥ 每 120 字 1 次；
  禁词表零命中（含「突然 / 忽然 / 仿佛 / 只见 / 缓缓 …」）。
- **去 AI 味门**：三连排比（不是A，不是B，不是C）全禁；叙述层推测词零容忍。
- **审稿门**：逐项验收，**待核实一律不算通过**；只报告，不自动改稿。

门禁实现与阈值唯一事实源：`workbench/backend/gates/`
（`density_gate` / `prose_gate` / `language_gate`）。
自检：`python -m pytest workbench/backend/tests -v`。

## 4. 详细规则（渐进披露）

本文件只放路由与门禁摘要，详细规则一律下沉：

- `skills/<name>/SKILL.md` — 技能正文、验收清单、改写手法
- `docs/AI小说创作工作台-开发文档.md` — 产品 / 架构 / 需求
- `docs/third-party-notices.md` — 第三方合规登记（引入前必查）
- `workbench/backend/gates/*.py` — 门禁阈值与词表
- `workbench/cli/check_license_compliance.py` — 禁用项目特征扫描

## 5. 技能同步

`skills/` 是**源目录**；同步器会把技能投影到 `.dsh/skills/`
（dsh 项目级技能根，发现 rank 100，格式 `<name>/SKILL.md`）。
本阶段只建目录骨架，同步器待实现；**禁止**手工编辑 `.dsh/skills/` 下的产物。
