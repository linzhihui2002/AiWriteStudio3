# evals/ — 金标准评测集（效果评测，不接入 pytest / CI）

本目录存放工作台的**金标准评测集**：用声明式用例描述「作者原话 → 期望执行 Agent /
期望写入目标 / 禁止写入目标 / 期望计划步骤」，供作者后续用**真实模型**做效果评测对照。

> **不接入 pytest / CI**：这里的用例不参与 `python -m pytest workbench/backend/tests`，
> 也不在 CI 里跑。**效果结论由作者真实评测给出；不把一次运行结果当作产品结论。**

## 子目录与脚本

| 路径 | 说明 |
|---|---|
| [`agent-scope/`](agent-scope/README.md) | 对话路由与写域评测集（用例 + 判定口径 + 跑测方法） |
| [`agent-scope/cases.json`](agent-scope/cases.json) | 用例数据（JSON） |
| [`agent-scope/README.md`](agent-scope/README.md) | **字段含义、判定口径、如何用真实模型跑一轮**（先读这个） |
| `run_cases.py` | 独立跑测脚本：`--dry-run` 只做离线字段校验；带 `--project-id` 做「期望 vs 实际」真实对照，不写文件、不改数据 |
| [`prose-quality/`](prose-quality/README.md) | 中文小说文学质量盲评：20 条原创短场景、两方案输出 schema、固定 seed 随机盲化、人工偏好与维度/事实/修改量/成本汇总 |
| `prose-quality/run_eval.py` | `--dry-run` 只校验原创场景；`prepare` 读取用户显式提供的输出，`score` 汇总人工裁决；不生成正文、不读取密钥或自动读取 `projects/` |
| `prose-quality/check_eval.py` | 标准库离线自检；校验盲化、方案映射、缺失与“两者都不好”口径、schema 和 hash；不接入 pytest / CI |

## 快速上手

```powershell
# 1) 离线校验用例字段（不调用路由与模型，退出码非 0 表示用例不合规）
python evals/run_cases.py --dry-run

# 2) 用真实模型跑一轮对照（需后端可跑、项目已配置可用模型与 API Key）
python evals/run_cases.py --project-id 1
```

脚本与用例**不依赖 PATH 上的全局 `dsh`，也不读写用户全局 `~/.dsh`**（遵守仓库隔离红线）。

新建评测集时，请在本目录下开子目录，并在上表登记，同时说明「验什么 / 判定口径 / 怎么跑」。

## 中文小说读感评测

```powershell
# 只校验 20 条原创短场景，不调用模型、密钥或小说项目
python evals/prose-quality/run_eval.py --dry-run
python evals/prose-quality/check_eval.py
```

准备实际两方案输出与人工盲评前，先读 [`prose-quality/README.md`](prose-quality/README.md)。
该入口不自动做付费生成；通过工作台受控生成取得输出，或提供已生成的 JSON，然后分开分享评审包与私有方案映射。
短场景练习的 300–650 汉字范围不改变正式章节硬门禁。未评、平局和两者都不好分别报告，没有明确偏好时不产生胜率。
