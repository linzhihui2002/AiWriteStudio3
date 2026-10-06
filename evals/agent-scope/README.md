# 金标准评测集 · Agent 路由与写域（agent-scope）

本目录是「AI 小说创作工作台」对话路由的**金标准评测集**，用于回答一个问题：

> 作者用自然语言提要求时，系统是否把活儿交给**正确的执行 Agent**、只授予**正确的写域**、
> 在跨材料时产出**有序的多步计划**？

它锚定的是本次修复的那个 bug：作者说「现在我需要完善设定」，路由却把 **大纲、状态、设定** 一起改了。
评测集用**声明式用例**把「输入 → 期望 Agent / 期望写入目标 / 禁止写入目标 / 期望步骤」固化下来，
供作者后续用**真实模型**做效果评测对照。

> **不接入 pytest / CI。** 效果结论由作者真实评测给出；**不把一次运行结果当作产品结论**。
> 本目录不参与 `python -m pytest workbench/backend/tests`，也不在 CI 里运行。
>
> 关联规范：`.trae/specs/upgrade-agent-routing-and-skills/spec.md` 的
> 「ADDED Requirements → 金标准评测集」。

## 文件

| 文件 | 作用 |
|---|---|
| `cases.json` | 用例集（JSON，便于人工阅读与 diff） |
| `README.md` | 本文件：字段含义、判定口径、如何用真实模型跑一轮 |
| `../run_cases.py` | 独立跑测脚本：`--dry-run` 离线校验；带 `--project-id` 则真实对照 |

## 用例字段

| 字段 | 类型 | 含义 |
|---|---|---|
| `id` | string | 用例唯一标识（kebab-case，稳定不轻易改名） |
| `tags` | string[] | 分类标签，如 `正例` / `越界反例` / `邻近意图干扰` / `兜底` / `只读` / `多步计划` |
| `input` | string | 作者原话（真实中文口吻，直接作为 `text` 传给路由） |
| `context` | object? | 可选上下文：`active_file`、`target_chapter`。**当前仅供人工用支持上下文的入口复现**；`routing_service.route()` 未透传它们，`run_cases.py` 只按 `input` 调用 |
| `expected_agent` | string | 期望主导执行 Agent（注册表内的名字） |
| `expected_write_targets` | string[] | 期望写域（产出材料）。判定口径见下 |
| `forbidden_write_targets` | string[] | **硬约束**：实际写域与它**交集必须为空**，命中即判越界 |
| `expected_plan_steps` | int ≥ 0 | 期望计划步数。`0` 表示**不得产生多步计划**（实际 `plan` 必须为空数组） |
| `expected_read_only_refs` | string[]? | 期望只读依据（可选）。**关键词路径也会返回依据材料**（识别依据介词「根据 / 按 / 依据…」与「X 里的 Y」位置限定），因此该字段在 `keyword` 与 `llm` 两条路径下都参与校验 |
| `expectation` | string | 一句话说明这条在验什么 |
| `notes` | string? | 判定口径补充、允许的取值集合、已知歧义等 |

写域元素只允许两种：**材料顶层目录名**（`章节` / `设定` / `大纲` / `状态` / `备忘录`），
或**本书相对路径**（如 `设定/世界设定.md`）。`run_cases.py --dry-run` 会机械校验这两点。

## 判定口径（逐条机械对照）

- **Agent**：实际 `agent` 严格等于 `expected_agent`。若 `notes` 写明了「允许取值集合」，
  则按集合判定（邻近意图干扰用例常这样放宽）。
- **写域 `expected_write_targets`（包含口径）**：
  - 非空数组 → 实际 `write_targets` **必须包含其全部元素**（子集判定）。
    允许 Agent 多写自己的主责材料，例如 `context-keeper` 主责 `状态` + `设定`，
    实际给 `["状态","设定"]` 也算命中 `["状态"]`。
  - 空数组 → 实际 `write_targets` **必须为空数组**（只读角色 / 兜底助理不得获得写域）。
- **禁止写域 `forbidden_write_targets`（硬约束）**：实际 `write_targets` 与它**交集必须为空**，
  任一命中即判「越界」。这是本评测集最重要的断言。
- **计划 `expected_plan_steps`**：`0` → 实际 `plan` 必须为空数组；`>0` → 实际 `plan` 长度必须 ≥ 该值
  （当前实现上限为 3 步）。关键词快路径也会按其「显式材料扫描」结果产出多步计划
  （`source=keyword` 时即可能 `plan` 非空），因此 `plan` 断言两条路径都可能命中。
- **只读依据 `expected_read_only_refs`**：实际 `read_only_refs` 须包含期望元素。
  **`keyword` 与 `llm` 两条路径都可能返回依据材料**（关键词路径识别依据介词与位置限定）；
  只有 `fallback`（未命中）与 `override`（显式指定 Agent）路径恒为空数组，此时该字段记为「本轮不适用」。
- **关键词快路径可能扩写域**：关键词路径会扫描消息里**点名的材料**并并入 `write_targets`
  （`备忘录/` 除外），还可能按显式点名的产出材料自动分步。因此部分用例的 `forbidden_write_targets` 差异在
  `source=keyword` 下就会暴露——这正是评测想看到的信号，请如实记录，不要为「凑命中率」改期望。
- **`scope_unresolved`**：只读角色（`reviewer`、`teardown-analyst`）与兜底助理的写域为空，
  路由会置 `scope_unresolved=true`。这表示**「无写域」而非「拒绝写入」**——
  与权限链里「本轮未声明产出材料」的拒绝语义不同，评测时不要把 `scope_unresolved=true` 当作失败。
- **`备忘录/` 是自由便签区**：写 `备忘录/` 下任何内容**永不判越界**；因此
  `forbidden_write_targets` 一律**不应包含** `备忘录`。本用例集里也不会出现把 `备忘录` 列为禁写的情况。

## `source` 字段（决定这轮靠什么判定）

路由结果里的 `source` 表明本次判定的来源，评测时**必须一并记录**：

| 取值 | 含义 |
|---|---|
| `llm` | 走了模型语义判定（两段式路由的 LLM 分支） |
| `keyword` | 关键词快路径唯一命中（零模型调用） |
| `fallback` | 关键词未命中（或判定失败回退）→ 交默认执行者 `writing-assistant` |
| `override` | 调用方/作者显式指定了 Agent |

> **提醒**：要评测 **LLM 判定质量**，项目必须已配置**可用模型与 API Key**（判定引擎走 `direct-api`）。
> 否则判定不可用，`source` 会落到 `keyword` / `fallback`。此时**含语义分步（`plan`）断言的用例**
> 可能因「关键词未识别出显式多类产出材料」而不命中，属**环境未就绪**，不是系统回归；
> 关键词路径自身也会返回 `read_only_refs` 与 `plan`，因此这两类断言并非只在 `llm` 下有效。

## 如何用真实模型跑一轮（手工步骤）

1. **启动后端（端口固定 8790）**，在仓库根执行：
   ```powershell
   python -m workbench.backend.app
   ```
   确认监听 `127.0.0.1:8790`（不要用 3080 / 8765 / 3456）。
2. **取一个项目 id**：
   ```powershell
   Invoke-RestMethod http://127.0.0.1:8790/api/projects
   ```
3. **逐条复现路由**（推荐接口；也不改任何数据）：
   ```powershell
   Invoke-RestMethod -Method Post http://127.0.0.1:8790/api/routing/route `
     -ContentType 'application/json' `
     -Body '{"project_id": 1, "text": "现在我需要完善设定"}'
   ```
   或直接 Python 调用（等价，`record=False` 不写路由操作日志）：
   ```powershell
   python -c "from workbench.backend.services import routing_service; import json; print(json.dumps(routing_service.route(1, '现在我需要完善设定', record=False), ensure_ascii=False, indent=2))"
   ```
4. **逐条比对**实际返回的 `agent` / `write_targets` / `read_only_refs` / `plan` / `source`
   与 `cases.json` 里的期望，按上面的判定口径记录命中 / 未命中。
5. **或让脚本自动对照**（打印「期望 vs 实际」表与命中统计，不写文件、不改数据）：
   ```powershell
   python evals/run_cases.py --project-id 1
   ```

先做一次**离线字段校验**（不调用路由与模型）：

```powershell
python evals/run_cases.py --dry-run
```

## 用例覆盖

共 16 条，覆盖：正例（设定 / 大纲 / 写作 / 状态 / 只读审稿 / 拆书 / 伏笔）、
越界反例（要求「完善设定」时禁写 `大纲/`、`状态/`、`章节/`；「照设定写大纲」时禁写 `设定/`）、
邻近意图干扰（设定 vs 状态、大纲 vs 角色成长、写作 vs 去AI味）、多步计划（先设定后卷纲）、
以及兜底与未声明写域。全部输入为自写中文作者口吻，未引用任何第三方项目文本。
