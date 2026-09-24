# 调研：OpenCodeReview (ocr) 工具与 Delegation 模式启用

## 调研元信息

| 项 | 内容 |
|---|---|
| 调研日期 | 2026-09-19 |
| 调研方式 | 本机安装实测（CLI 运行验证）+ 本地安装包 README 审读 + 官方文档 WebFetch（configuration / delegate 两篇） |
| 调研主题 | 阿里开源 AI 代码评审 CLI `@alibaba-group/open-code-review` 的功能、配置体系与无 API Key 的 Delegation 模式 |
| 对标本项目 | 为本项目（及跨项目）建立可复用的 AI 代码审查流程；本项目已 git init 并完成首次提交 |
| 归档位置 | `d:\desktop\AiWriteStudio3\docs\调研归档\` |
| 产出依据 | 全局 Skill `ocr-code-review`（`C:\Users\30332\.trae-cn\skills\ocr-code-review\SKILL.md`）的原始事实来源 |

**证据等级说明**：标注【实测】的为本机运行验证（等级 A）；标注【文档】的来自官方 README/文档（等级 B）；标注【推断】的为分析结论（等级 C）。

---

## 1. 工具定位（事实）

- OpenCodeReview（OCR）是阿里巴巴开源的 AI 代码评审 CLI，源自阿里内部官方 AI 代码评审助手，Apache-2.0 协议。【文档】
- 工作原理：读取 git diff → 按确定性工程逻辑做文件筛选/分组/规则匹配 → 交给带工具调用能力的 LLM 生成行级评审评论；另有 `ocr scan` 整文件扫描（无需 diff）。【文档】
- 官方基准（AACR-Bench，50 仓库 / 200 PR / 10 语言）：相对通用 Agent（Claude Code），同模型下 Precision 与 F1 显著更高，token 消耗约 1/9，Recall 更低（刻意以精度换噪音）。【文档，未实测复验】
- 前置要求：Git >= 2.41。【文档】

## 2. 本机安装与验证（实测）

| 项 | 结果 | 验证方式 |
|---|---|---|
| npm 包 | `@alibaba-group/open-code-review@1.12.7` 全局安装 | `npm ls -g` 实测 |
| CLI | `ocr --version` → `open-code-review v1.12.7 (85cecfe) windows/amd64` | 实测 |
| 平台二进制 | 通过 optionalDependencies 按平台分发（本机 `ocr-win32-x64`），postinstall 自动下载 | 安装包 package.json 审读 |
| Git | 2.54.0，满足 ≥2.41 | 实测 |
| LLM 配置 | 无 `~/.opencodereview/config.json`（即 `C:\Users\30332\.opencodereview\config.json`） | 实测 |
| 子命令 | completion / config / delegate / llm / review / rules / scan / session / version / viewer | `ocr --help` 实测 |

## 3. Delegation 模式（本机采用方案，实测跑通）

委托模式：OCR 只做确定性工程（文件筛选、规则解析），**OCR 端不调用任何 LLM**，实际审查由宿主 AI 代理用自己的模型完成。专为订阅制 AI 编码代理设计，无需配置 API Key。【文档 + 本机实测】

五步工作流（官方文档流程，本机逐步验证）：

1. `ocr delegate preview [--from <ref> --to <ref>] [-c <hash>]` → 输出 mode（workspace/range/commit）、ref 元数据（range 模式含 merge_base）、可审查文件列表（路径/状态/增删行数）、排除文件及原因。【实测：对本项目首提交输出 1 reviewable / 6 total，md 文件按 unsupported_ext 排除、.vscode/settings.json 按 provider_directory 排除】
2. `ocr delegate rule <path...>` → 按规则内容分组的审查清单（规则含 Correctness/Security/Performance/Maintainability/Test Coverage 维度）。【实测】
3. 获取 diff 用 git 而非 ocr：Range 模式 `git diff <merge_base>..<to> -- <path>`；Commit 模式 `git show <commit> -- <path>`；Workspace 模式 `git diff HEAD -- <path>`，新增未跟踪文件直接读内容。【实测】
4. 宿主代理逐文件审查：diff + 规则组作清单，按需读上下文。
5. 分级报告：Critical/High（bug、安全、数据丢失）必报；Medium（性能、错误处理）附上下文；Low（风格）默认丢弃。【文档】

## 4. OCR 自驱模式（未启用，备查）

- `ocr review`：diff 评审（workspace / --from+--to merge-base / --commit 单提交三种模式），支持 `--format json|sarif`、`--output`、`--preview`、`--exclude`、`--effort low|medium|high`、`--resume`、`--background` 业务上下文、`--max-tokens-budget` 总量上限。【文档】
- `ocr scan`：整文件扫描（`--path` 指定目录/文件，支持 `--batch by-language|by-directory`、`--no-plan` 等开关），无需 diff 历史。【文档】
- 配置持久化于 `~/.opencodereview/config.json`，可用 `ocr config set <key> <value>` 非交互写入：
  - 内置 provider 20+，均预置 Base URL 与协议，API Key 支持环境变量回退（如 `DASHSCOPE_API_KEY`、`DEEPSEEK_API_KEY`、`MOONSHOT_API_KEY`、`Z_AI_API_KEY`、`ARK_API_KEY`、`ANTHROPIC_API_KEY`、`OPENAI_API_KEY`）；自定义 provider 需 `url` + `protocol`（anthropic/openai/openai-responses/anthropic-bedrock），本地 Ollama 亦按自定义 provider 配置。【文档】
  - `ocr config set language 中文` 可设评审输出语言；`ocr llm test` 验证连通。【文档】
- 若未来用户提供 API Key，切换仅需三条 `ocr config set` 命令（见全局 Skill）。

## 5. 与本项目的关系（推断）

- 本项目工作区已于 2026-09-19 git init 并完成首次提交（`7c5e732`），后续所有变更可用 `ocr delegate preview`（workspace 模式）筛查并由会话内代理执行审查。
- `projects/` 下 20 个第三方参考仓库各自独立成库，可分别在其目录内用同样流程审查。
- 本工具与 dsh（DeepSeekHarness）无冲突：ocr 是独立 CLI，不涉及 profile/插件体系，不写工作区文件（除会话记录外）。

## 6. 来源链接清单

| 来源 | 内容 | 是否实测 |
|---|---|---|
| https://github.com/alibaba/open-code-review | 项目仓库与 README | 部分（README 审读自本地安装包同文件） |
| https://open-codereview.ai/docs/configuration | 配置体系（provider 表、config.json、语言、超时） | 否（文档） |
| https://open-codereview.ai/docs/delegate | Delegation 模式工作流 | 是（五步全部本机验证） |
| 本地 `C:\Users\30332\AppData\Roaming\npm\node_modules\@alibaba-group\open-code-review\` | package.json / README.md / bin | 是 |
