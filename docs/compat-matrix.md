# 兼容矩阵：AI 小说创作工作台 × DeepSeekHarness（dsh）

> 本文件记录工作台内置 dsh 的三项关键能力**实测**结论，作为后续对话/模型配置架构的决策依据。
> 所有结论均为**本机实测**，包含失败项与与预期不符项，未做美化。

## 0. 元信息

| 项 | 值 |
|---|---|
| 验证日期 | 2026-09-19 |
| Workbench 版本 | `0.1.0`（`workbench/backend/config.py` 的 `VERSION`） |
| dsh 版本 | `0.1.0-rc.8`（`@deepseek-ai/dsh`，**rc 预发布**） |
| dsh 位置 | `workbench/vendor/dsh/node_modules/.bin/dsh.cmd`（vendor 内置、版本锁定） |
| DSH_HOME | `.workbench/dsh-home`（独立隔离，非用户 `~/.dsh`） |
| 测试模型路由 | FluxLane 网关 `https://fluxlane.cn/v1`，模型 `glm-5.3-flash`（经 `llm-pi-ai` 手工声明路由） |
| 测试环境 | Windows；Node ≥ 20；调用均注入独立 `DSH_HOME` |
| 隔离红线 | 全程未写入用户 `C:\Users\30332\.dsh`；未执行 `npm i -g`；未执行 `dsh plugin` |

### 测试用模型路由配置（`.workbench/dsh-home/settings.yaml`）

```yaml
agent-default-model:
  provider: fluxlane
  model: glm-5.3-flash

llm-pi-ai:
  providers:
    fluxlane:
      displayName: FluxLane
      api: openai-completions
      baseURL: https://fluxlane.cn/v1
      apiKeyEnv: FLUXLANE_API_KEY        # 密钥引用，明文存于同目录 .credentials.yaml
      compat:
        supportsDeveloperRole: false
        maxTokensField: max_tokens
      models:
        - id: glm-5.3-flash
          name: GLM 5.3 Flash
          contextWindow: 131072
          maxTokens: 8192
```

---

## 1. 兼容矩阵总表

| # | 能力 | Workbench | dsh | 验证状态 | 结论 | 破坏性变更 / 风险备注 |
|---|---|---|---|---|---|---|
| B1 | headless 多轮会话续接（`--resume`） | 0.1.0 | 0.1.0-rc.8 | ❌ **不支持**（实测） | headless 无续接入口；多轮只能"每轮完整重放上下文" | 每次调用新建会话并落盘；无引用回会话的入口；rc 版本随时可能变 |
| B2 | `--patch` 运行时覆盖指定模型 | 0.1.0 | 0.1.0-rc.8 | ✅ **可行**（实测） | `- id: agent-default-model` + `config.{provider,model}`；**但优先级低于 settings.yaml 用户层** | patch 不做深合并，整段替换 config；settings 用户层会盖掉 `--patch`（易踩坑） |
| B3 | headless stdout 协议 | 0.1.0 | 0.1.0-rc.8 | ✅ **已确认**（实测） | stdout = 最后一条非空 assistant 文本 + `\n`；stderr 失败时才有一行；成功退出码 0 / 失败 1 | 无 JSON 模式、无日志流；失败时 stdout 仍可能是一个空行 |
| A | 完整性自检脚本 | 0.1.0 | 0.1.0-rc.8 | ✅ 跑通（实测） | `workbench/cli/selfcheck.py` 基线/比对两模式均正常，退出码语义正确 | 基线落 `.workbench/dsh_baseline.json`；跳过 `profiles/**/node_modules` |

---

## 2. B1 · headless 会话续接能力 —— ❌ 不支持（决定性结论）

### 2.1 问题

`dsh-headless` 的 README 明确写「只提交一个任务，runner 没有用于交互式后续输入的 surface」，而 `--resume` 属于 terminal app。多轮对话能否实现？

### 2.2 实测证据

**证据 1：`--resume` 被 headless 直接拒绝**

```
> dsh --profile headless --resume abc123 "hello"
error: unknown option '--resume'
退出码 = 1
```

**证据 2：`dsh --profile headless --help` 只暴露一个位置参数与 `-h/--help`**

```
Usage: dsh --profile headless [options] [task...]
Answer one task, print the final assistant message, and exit.

Arguments:
  task        the task text; multiple words are joined by spaces

Options:
  -h, --help  show this help
```

根帮助中 `--resume` 的示例明确标注属于 **tui/terminal app**，不属于 headless。

**证据 3：源码确认每次创建全新会话**

`workbench/vendor/dsh/node_modules/@deepseek-ai/dsh-headless/lib/index.js`：

```js
const { agent } = await agents.create({
    sessionId: SessionId(`session-${randomUUID()}`),   // 每次随机 UUID，无入参
    meta: { cwd: process.cwd() },
    agentOptions: { provider: selection.provider, model: selection.model },
    ...
});
```

会话 id 硬编码为随机 UUID，**没有任何来自命令行的会话 id 入口**。

**证据 4：跨进程无上下文携带（端到端行为实测）**

| 轮次 | 任务 | stdout | 退出码 |
|---|---|---|---|
| 第 1 轮 | `Remember this codeword for later: ZEBRA-7731. Just reply OK.` | `OK` | 0 |
| 第 2 轮 | `What was the codeword I told you earlier? If you have no memory of it, reply exactly UNKNOWN.` | `UNKNOWN` | 0 |

第 2 轮无法回忆第 1 轮的暗号 → **确认无上下文续接**。

**证据 5：会话确实被持久化，但无入口引用回去**

实测 6 次 headless 调用后，`.workbench/dsh-home/sessions/<workspace-编码目录>/` 下新增 6 个 `session.jsonl.zstd`（每次运行 1 个），文件非空（约 5–18 KB）。

即：**数据在盘上，但没有 CLI surface 能把某个已存会话重新加载进新一轮对话**（`sessions.flush()` 只负责写盘）。

### 2.3 结论（决定对话功能实现方式）

- **headless 不支持多轮续接，也不支持 `--resume <sessionId>`。**
- 工作台的对话功能**只能靠「每轮完整重放上下文」**：把系统提示 + 全部历史消息 + 本轮用户输入拼成单个 task 文本，作为**一次** headless 调用提交。
- 代价：每轮 token 成本随对话轮数**线性增长**（无服务端会话缓存复用），需在工作台侧设计上下文裁剪 / 摘要压缩。
- 备选（未验证、需另立任务评估）：`dsh web`（profile `web`，默认端口 3080）走 Host/ApiProxy，可能具备真正的会话续接与流式协议；但会引入端口占用与 Web runtime 依赖，与"零冲突共存"目标存在张力。

---

## 3. B2 · `--patch` 运行时覆盖能否指定模型 —— ✅ 可行，但有优先级陷阱

### 3.1 相关配置键名（来自 `--dump-config`）

```
- id: agent-default-model
  name: '@deepseek-ai/dsh-agent-default-model'
  config:
    provider: deepseek-official
    model: deepseek-v4-flash
```

- **模型选择配置键 = `agent-default-model`**，字段为 `provider` + `model`（可选 `reasoningEffort`）。
- provider 路由声明键 = `llm-pi-ai.providers.<route>`（`dsh-llm-pi-ai` 通用多提供方适配器）。
- 同一 `agent-default-model` 也是 **settings 分节名**（`dsh-agent-default-model` 通过 `installSettingsSection` 注册）。

### 3.2 `--patch` 用法与格式

`dsh --patch <path>`（可重复）接受一份 **顶层 YAML 数组**的 patch 列表；条目按 `id` 定位条目并**整段替换其 `config`**（不做深合并，未改字段也要重述）。

```yaml
# patch-model.yml
- id: agent-default-model
  config:
    provider: fluxlane
    model: glm-5.3-flash
```

### 3.3 实测证据

| 场景 | settings.yaml 是否含 `agent-default-model` | `--patch` 指向 | 结果 | 退出码 |
|---|---|---|---|---|
| 1 · 合成树 | 含 | bogus 模型 | `--dump-config` 显示 `agent-default-model.model = model-that-does-not-exist-xyz` | 0 |
| 2 · 运行 | 含（fluxlane/glm-5.3-flash） | bogus 模型 | **仍成功输出 `OK`** → settings 用户层**覆盖**了 `--patch` | 0 |
| 3 · 运行 | **不含** | bogus 模型 | 失败：stderr `dsh: UNKNOWN_MODEL: pi-ai provider "fluxlane" has no configured model "model-that-does-not-exist-xyz"`，stdout 为空行 | 1 |
| 4 · 运行 | **不含** | 有效模型 | 成功输出 `PATCHOK` | 0 |

场景 2 与 3/4 的对比证明：

- `--patch` **确实能**覆盖 `agent-default-model`（场景 1、3、4）；
- 但 **settings.yaml 的用户层优先级高于 `--patch` 组合层**（场景 2）——因为 `agent-default-model` 的 settings 用户选择会叠加在组合 `base`（含 `--patch` 结果）之上。

### 3.4 结论与工作台用法建议

- **`--patch` 可以指定模型**：`- id: agent-default-model` + `config: {provider, model}`。
- **但不要指望它压过 settings.yaml**：只要 `settings.yaml` 里写了 `agent-default-model`，`--patch` 就会被静默盖掉（**无告警**，极易误判）。
- 工作台实现"每个 Agent 单独配模型"的可行路径（按推荐度）：
  1. **每 Agent 一份独立 DSH_HOME（或独立 settings.yaml）**，用 settings 分节 `agent-default-model` 指定——语义最明确、优先级最高；
  2. 若复用同一 DSH_HOME，则**不得**在 settings.yaml 固定 `agent-default-model`，改用 `--patch` 按次注入；
  3. 注意 `--patch` 整段替换 config：patch 中必须重述 `provider` 与 `model` 两个字段。

---

## 4. B3 · headless stdout 协议 —— ✅ 已确认

### 4.1 实测结果

| 观测项 | 结果 |
|---|---|
| stdout | **仅最后一条非空 assistant 文本**，末尾一个 `\n`；无日志、无流式片段、无工具调用输出 |
| stderr | **成功时为空**；失败时为单行 `dsh: <CODE>: <message>` |
| 退出码 | 成功（`turn/end` reason = `completed`）= `0`；失败 = `1`；用法错误（缺 task / 未知选项 / 缺 `--profile`）= `1` |
| 产物落盘 | ✅ 可行：要求模型用文件工具写 `.workbench/b3_artifact.txt`，文件内容正确写入；stdout 仍只有 `DONE` |

**实测样例**

```
# 最小任务
> dsh --profile headless "Reply with exactly: OK"
stdout: "OK\n"    stderr: ""    exit=0

# 工具 + 落盘任务
> dsh --profile headless "Create the file .workbench/b3_artifact.txt with exactly the content HELLO_FROM_DSH (use your file tools). Then reply with exactly: DONE"
stdout: "DONE\n"  stderr: ""    exit=0    产物文件内容 = "HELLO_FROM_DSH"

# 模型错误（--patch 指向不存在的模型）
stdout: "\n"      stderr: "dsh: UNKNOWN_MODEL: pi-ai provider \"fluxlane\" has no configured model \"...\""   exit=1

# 缺 task
stdout: ""        stderr: "error: a task is required, for example: dsh --profile headless \"run the tests\""   exit=1

# 缺 --profile
stderr: "error: --profile <name> is required"   exit=1
```

对应源码（`dsh-headless/lib/index.js`）：

```js
io.stdout.write(outcome.text + "\n");
if (outcome.reason?.kind === "error") io.stderr.write(`dsh: ${outcome.reason.error.code}: ${outcome.reason.error.message}\n`);
io.exit(outcome.reason?.kind === "completed" ? 0 : 1);
```

### 4.2 可稳定解析的调用协议建议

1. **调用形态**：`dsh --profile headless "<task>"`；task 必须作为**单个位置参数**（多个词用引号包成一个参数）。启动器 flag 必须写在最前。
2. **成功判定**：`退出码 == 0` **且** `stderr` 为空。
3. **结果解析**：`stdout` 整体 `strip()` 即最终答案；**不要**按行/流式解析（无流式输出、无增量）。
4. **失败判定与取错**：`退出码 != 0` → 取 `stderr` **首行**作为错误信息；格式为 `dsh: <CODE>: <message>`，`<CODE>` 可用于错误分类（如 `UNKNOWN_MODEL`）。
5. **陷阱**：失败时 stdout 仍可能输出一个**空行**（`text + "\n"`，text 为空），**不可**用"stdout 非空"判定成功。
6. **产物落盘**：dsh headless **不会**把产物写文件；需在 task 文本中**显式要求模型使用文件工具写入指定路径**，工作台再读取该文件。这与工作台原设计"产物落盘、stdout 仅日志"**方向相反**——现状是"答案在 stdout、无日志"。工作台需二选一：
   - (a) 让模型写产物到指定文件，stdout 只回一个简短确认（推荐，契合工作台落盘设计）；
   - (b) 直接以 stdout 作为答案正文。
7. **隔离**：调用必须注入独立 `DSH_HOME`，且使用 vendor 内置 dsh 绝对路径（经 `workbench/backend/engine/dsh_paths.py`）。
8. **无结构化输出**：dsh headless 当前**没有** JSON 输出模式或 `--output` 参数；如需结构化，只能靠 prompt 约束 + 工作台侧解析。

---

## 5. 附：完整性自检脚本（任务 A）

`workbench/cli/selfcheck.py` —— 对用户全局 `~/.dsh` 的**只读**基线与比对。

| 模式 | 命令 | 行为 |
|---|---|---|
| 记录基线 | `python workbench/cli/selfcheck.py --baseline` | 采集关键项 SHA-256 + mtime，写 `.workbench/dsh_baseline.json` |
| 比对 | `python workbench/cli/selfcheck.py --verify` | 比对当前状态与基线，列出差异并告警 |
| 默认 | `python workbench/cli/selfcheck.py` | 基线不存在则记录，存在则比对 |

- 关键项：`settings.yaml`、`profiles/`（跳过 `node_modules` 依赖树）、`.agent-presets/`、`storages/workspace.json`；外加 `npm ls -g --depth=0` 全局包快照。
- 退出码：一致 `0`；不一致 `1`（含基线缺失无法比对）。
- **绝不写入 / 修复用户文件**；差异时仅输出「最近可能原因」排查提示。

**实测结果（2026-09-19）**

- `--baseline`：退出码 0；记录 `settings.yaml`、`profiles/` 7 个文件、`.agent-presets/` 5 个文件、`storages/workspace.json`、全局 npm 4 个包。
- `--verify` / 默认模式：全部关键项 PASS，退出码 0。
- 差异检测路径验证（篡改**基线副本**，未触碰用户目录）：正确报出 2 处不一致（`settings.yaml` 内容哈希变更、全局 npm 新增包），退出码 1，并打印排查提示；随后基线已还原。

---

## 6. 破坏性变更备注与待验证项

### 6.1 风险备注

- **dsh 为 `0.1.0-rc.8` 预发布版本**：headless stdout 协议、`--patch` 语义、`agent-default-model` 配置键均可能在小版本间变化；升级前必须重跑本矩阵的三项实测（Contract Test）。
- **`--patch` 无深合并**：按 id 定位的 patch 会**整段替换** `config`，必须重述要保留的字段。
- **settings 用户层 > `--patch`**：二者同时存在且冲突时，settings 静默胜出，**无任何告警**，是本矩阵中最易踩的坑。
- **headless 每次调用都会在 `DSH_HOME/sessions` 落一个持久化会话文件**：工作台需配套清理/归档策略，避免无限增长。
- **无会话续接 → 上下文重放成本随轮数线性增长**：需在工作台侧做上下文裁剪/摘要。
- **`--profile` 为必填**：裸 `dsh --dump-config` 直接报错退出码 1。

### 6.2 待验证项（本任务未覆盖）

- `dsh web`（profile `web`）是否提供真正的多轮会话与流式协议（含 3080 端口占用影响）。
- 自定义 profile（`ai-novel-workbench`）在 headless 组合下的行为是否与 `headless` 模板完全一致。
- 会话文件的读取/重放可行性（`session.jsonl.zstd` 解压后能否人工拼回上下文）。
- `reasoningEffort` 等 `agent-default-model` 可选字段在 FluxLane 网关上的实际支持情况。
