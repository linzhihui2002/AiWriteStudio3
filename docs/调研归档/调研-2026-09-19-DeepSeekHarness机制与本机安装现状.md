# 调研：DeepSeekHarness 机制与本机安装现状

## 调研元信息

| 项 | 内容 |
|---|---|
| 调研日期 | 2026-09-19 |
| 调研方式 | 本机静态检查（PowerShell 文件系统检查）+ 安装包源码审读（含子代理 Explore 对 dsh-base / dsh-agent-instructions / dsh-skill-filesystem 源码的定位） |
| 调研主题 | DeepSeekHarness（@deepseek-ai/dsh）的项目级配置发现机制、本机安装现状、与工作台类应用的共存约束 |
| 对标本项目 | 直接支撑《AI小说创作工作台-开发文档》第 2 章（现状）、第 3 章（冲突分析与隔离）、第 6 章（集成方案） |
| 归档位置 | `d:\desktop\AiWriteStudio3\docs\调研归档\` |
| 产出依据 | 本文件为主开发文档冲突隔离方案的唯一原始事实来源 |

**证据等级说明**：除特别标注外，本文所有结论均为**本机实测或安装包源码直读**（等级 A）；推断性结论会明确标注（等级 C）。

---

## 1. DeepSeekHarness 是什么（事实）

- `@deepseek-ai/dsh` 是 DeepSeek 官方的 Agent Harness 启动器（npm 包），本机安装版本 `0.1.0-rc.8`（**rc 预发布版本**）。
- 架构：`dsh` 命令启动 **profile**；profile 由多个插件组合包（bundle）patch 层按顺序叠加，再应用用户覆盖配置。
- 入口模式：
  - `dsh --profile <name>`：启动 `$DSH_HOME/profiles/<name>` 的 profile
  - `dsh --profile headless "job"`：运行全新持久化会话，打印最终答案后退出（**自动化关键入口**）
  - `dsh web`：`--profile web` 别名（Web 界面，默认端口 **3080**）
  - `dsh plugin --profile <name> <pnpm args>`：管理 profile 插件
- 配置层叠顺序（源码注释确认）：`dsh.profile.bundles` 各组合包 patch → profile 的 `cordis.patch.yml` → `$DSH_HOME/cordis.patch.yml` → `--patch` 运行时覆盖。
- 运行命令所在目录作为默认 workspace 根目录。
- 证据：`C:\Users\30332\AppData\Roaming\npm\node_modules\@deepseek-ai\dsh\README.zh.md`（全文审读）

## 2. 本机安装现状（实测）

### 2.1 安装位置

| 项 | 值 | 证据 |
|---|---|---|
| 全局 npm 包 | `@deepseek-ai/dsh@0.1.0-rc.8` | `npm ls -g` 输出 |
| 命令入口 | `C:\Users\30332\AppData\Roaming\npm\dsh.ps1` | `Get-Command dsh` |
| 包目录 | `C:\Users\30332\AppData\Roaming\npm\node_modules\@deepseek-ai\dsh` | 文件系统 |
| DSH_HOME | `C:\Users\30332\.dsh` | settings.yaml 等存在 |

全局 npm 同装：`@anthropic-ai/claude-code@2.1.185`、`pnpm@11.22.0`。

### 2.2 DSH_HOME 内容（`C:\Users\30332\.dsh`）

- `settings.yaml`（**用户已深度定制**）：
  - `llm-pi-ai.providers`：4 个 provider（cun→wintoken.dev、opencodetest→opencode.ai/zen、aim→mzlone.top、tokenrouter→api.tokenrouter.com），含模型清单
  - `agent-default-model`：tokenrouter / z-ai/glm-5.3-free
  - `llm-deepseek.baseURL`：http://localhost:3456/v1（本地代理）
  - `describe-image`：mzlone.top + 明文 apiKey（**用户把 Key 明文存在此文件**）
  - `pet`、`skin-*`、`ui-*` 等个性化配置
- `profiles/web/`：唯一 profile，pnpm 工程，捆绑 9 个 bundle（`@deepseek-ai/dsh-base`、`@deepseek-ai/dsh-web-app`、`dshmarket`、`@linxin666/dsh-web-ui-all`、`dsh-recall-plugin`、`dsh-prompt-enhancer`、`@js2hou/dsh-mcp-manager`、`dsh-free-search`、`dsh-vision-router`）
- `.agent-presets/liangshen/`：用户自建"梁神模式"预设（preset.yml + 约 360 行 agent.cordis.yml + 自定义插件 tool-bootstrap.mjs / custom-bash.mjs）。**信任级别等同 shell 访问**（dsh-web-app cordis.patch.yml L431-445 注明）
- `storages/workspace.json`：注册了 6 个 workspace，含 `D:\desktop\AiWriteStudio3`（2026-09-13 创建，6 个会话）——**本机 dsh 处于活跃使用状态**
- `sessions/`：按 workspace 路径分目录，`session-*.jsonl.zstd` 压缩存储
- MCP：由 `@js2hou/dsh-mcp-manager` 管理，条目写入 `profiles/web/cordis.patch.yml`（当前仅 vision-router 启用）

### 2.3 已知端口

- dsh web 默认端口 **3080**（来自 AiWriteStudio2 PRD 6.1 节既有结论 + FR-MODEL-000B）
- 用户 `llm-deepseek.baseURL` 指向 localhost:3456（本地代理占用）

## 3. 项目级配置发现机制（源码级，等级 A）

### 3.1 指令文件注入（dsh-agent-instructions）

源码：`dsh\node_modules\@deepseek-ai\dsh-agent-instructions\lib\index.js`

- **项目根定位**（findProjectRoot，L473-481）：从会话 cwd 逐级向上找第一个含 `projectRootMarkers`（默认 `['.git']`）的目录；找不到回退 cwd。
- **候选链**（L15-17）：`AGENTS.md` / `CLAUDE.md`；本地覆盖 `AGENTS.local.md` / `CLAUDE.local.md`
- **加载顺序**：先 `$DSH_HOME/AGENTS.md`（本机不存在），再从项目根到 cwd **每一级目录**依次读；同目录内容去重（SHA-1 相同折叠）
- **预算**：`maxBytes` 默认（本机 liangshen 预设设 65536），单文件上限 1MiB
- **嵌套发现**：无文件 watcher；只有 read/write/edit 工具触达更深目录后才注入新发现的指令
- `instructionFileCandidates` / `projectRootMarkers` 均为插件配置项，可经 cordis patch 覆盖

### 3.2 技能发现（dsh-skill-filesystem）

源码：`dsh\node_modules\@deepseek-ai\dsh-skill-filesystem\README.zh.md`

发现根按 rank 排序：

| Rank | 来源 | 路径 |
|---|---|---|
| 100 | project-dsh | `<projectRoot>/.dsh/skills` |
| 200 | project-agents | `<projectRoot>/.agents/skills` |
| 300 | custom | 配置 `customSkillDirs` |
| 400 | user-dsh | `<dshHome>/skills`（跳过 `.system`） |
| 500 | user-agents | `$DSH_AGENTS_HOME`/`~/.agents` 下 skills |

- 格式：目录 bundle `<root>/<name>/SKILL.md` 或平铺 `<root>/<name>.md`；**不支持嵌套 `**/SKILL.md`**
- Frontmatter：必填 `name`（kebab-case）+ `description`；可选 `whenToUse`、`metadata`、`disable-model-invocation`、`user-invocable`
- 正文每次调用时重读，无缓存失效问题

### 3.3 关键否定结论

| 结论 | 证据 |
|---|---|
| dsh 核心不认识 `.harness/` 目录 | 全安装树 grep `\.harness` 仅误命中变量名；novel-harness 的 `.harness/` 全靠其根目录 AGENTS.md 间接生效 |
| 无项目级 MCP 配置机制 | dsh-base 包 grep `mcp` 零命中；settings.yaml 无 mcp 键；MCP server = cordis 组合层插件实例（dsh-mcp-client README），只能 profile 级配置 |
| 无项目级 cordis patch | `lib\profile-boot-DG5t9aNs.js` 无 `process.cwd()` 调用，层级注释明确仅到 `$DSH_HOME/cordis.patch.yml` + `--patch` |
| workspace 注册表不读文件夹配置 | `storages/workspace.json` 仅存路径 + 会话分组 |
| 本机无 `~/.dsh/skills`、无 `D:\desktop\AiWriteStudio3\.dsh` | Test-Path 实测 |
| `~/.agents` 存在但为空 | 实测 |

### 3.4 `$DSH_HOME` 可重定向（隔离关键，等级 A）

README 明确 profile 位于 `$DSH_HOME/profiles/<name>`，`$DSH_HOME/cordis.patch.yml` 为环境变量引用 → 设置环境变量 `DSH_HOME` 指向其他目录即可让 dsh 使用独立 Home（headless/web profile 首次使用时会从随附模板自动初始化）。

### 3.5 对 AiWriteStudio3 的具体影响

- **本项目根无 `.git`**：dsh 会话从 AiWriteStudio3 启动时 projectRoot 回退为 cwd；而 `projects\manhai934__novel-harness` 子目录有 `.git`，会被判定为独立项目根 → 技能/指令发现范围分裂。**在本项目根建 `.git` 可统一解析**。
- 参考项目的根级 `skills/` 文件夹（如 novel-harness 的 `skills/`）**不在** dsh 技能扫描路径内。

## 4. 共存硬性要求的既有基线（继承，等级 A）

来自 AiWriteStudio2 PRD（`d:\desktop\AiWriteStudio2\reports\PRD-AI-Novel-Workbench.md` FR-MODEL-000/000A/000B）：

- 工作台使用**内置且锁定版本**的 DSH，不调用 PATH 中的全局 `dsh`
- 使用**独立 DSH_HOME** 和专用 profile
- 默认 stdio/JSON-RPC，不依赖 DSH Web 的 3080 端口
- **不读取、修改或删除**用户 `~/.dsh` 的插件、会话、配置和凭据
- 小说项目、正文和 Story Bible 不得保存到 DSH Home
- 用户电脑已有 DSH / 占用 3080 时，工作台仍必须正常启动
- 升级前执行 DSH Contract Test；记录兼容矩阵
- 模型配置可显式导入已有 DSH 的**非敏感**配置，但不得自动复制用户 API Key

## 5. 来源清单

| 来源 | 类型 | 是否实测 |
|---|---|---|
| `C:\Users\30332\AppData\Roaming\npm\node_modules\@deepseek-ai\dsh\README.zh.md` | 官方文档 | 是（直读） |
| `dsh\node_modules\@deepseek-ai\dsh-agent-instructions\lib\index.js` | 源码 | 是（直读 L15-17/L473-481/L571/L1073-1084） |
| `dsh\node_modules\@deepseek-ai\dsh-skill-filesystem\README.zh.md` | 官方文档 | 是（直读） |
| `C:\Users\30332\.dsh\settings.yaml` | 用户配置 | 是（直读） |
| `C:\Users\30332\.dsh\storages\workspace.json` | 运行时数据 | 是（直读） |
| `C:\Users\30332\.dsh\profiles\web\*` | 用户环境 | 是（子代理审读） |
| `npm ls -g --depth=0` / `Get-Command dsh` | 系统命令 | 是 |
| AiWriteStudio2 PRD 基线两份 | 既有文档 | 是（直读关键节） |
| dsh headless 实际运行行为（流式输出细节、会话恢复协议） | 待验证 | **否，标注为实施期 Contract Test 验证项** |
