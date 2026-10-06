# 对话工作区真实模型对照

这套评测独立于 pytest/CI，会调用已配置的真实模型。两组使用相同供应商、模型、temperature、输出上限、合成材料与作者回应策略。改造前代码来自开工时保存的未提交工作树 archive，改造后使用启动时冻结的当前实现；native dsh 版本保持锁定。

```powershell
python evals/chat-workspace/check_comparison_support.py
python evals/chat-workspace/run_comparison.py --output .workbench/chat-comparison-completion-new --freeze-only --max-output-tokens 8192
python evals/chat-workspace/run_comparison.py --output .workbench/chat-comparison-completion-new --max-parallel 2 --max-output-tokens 8192
python evals/chat-workspace/summarize_comparison.py .workbench/chat-comparison-completion-new
```

12 组覆盖设定交付、大纲交付、有序复合任务、否定写域、依据与产出分离、只读审稿、兜底讨论与便签、作者事实连续性、历史选项、当前文件选区、请求幂等和故障续接。每组至少两轮，故障续接三轮。第12组在首步完成且收据/文件hash核验、第二步模型请求开始前注入 `EVAL_TRANSPORT_FAULT`；保存 `first_step_verified` 等证据，观察续接复用首步与仅一次实际写入。基线无checkpoint时仅记录原生首步完成与实际文件。未达到故障点不能算续接验收；历史版本首个工具成功后即断的记录仅证明中断，不能证明已完成步骤被跳过。

每个 worker 使用自己的 SQLite、合成书籍和 DSH_HOME，通过 `dsh_paths.py` 调用 vendor，不启动监听端口。供应商密钥经 stdin 到 worker，存于隔离 DPAPI 存储及环境变量；不写明文评测结果和原生配置。启动时冻结双方代码、实际Agent、共用skills/rules/workflows/templates及harness，每份目录/文件与vendor锁文件记hash；worker只导入冻结副本。目录参数/用例/源码/资产变化或冻结副本被改会拒绝复用；旧实验目录不续写，状态不明的部分worker禁止自动重放。`--freeze-only`不加载密钥或发送模型请求。确认额度不足后停止尚未开始的worker，保留失败与未执行项。

`manifest.json`固定参数、归档与冻结目录指纹；`result.json`保存逐轮路由、写域、运行状态、文件变更、完整事件和本轮意图调用的usage范围；`observations.json/md`汇总实际证据。汇总器只读隔离事件库，覆盖超过500条事件的长回复；不调用模型。`native_execution_usage`与SQLite `task_type='意图判定'`的`routing_usage`分别提供，避免遗漏路由成本或重复计入执行。旧记录缺逐轮usage边界时仅补case总量，不伪造逐轮归属。缓存字段不推断计费。离线`check_comparison_support.py`检验冻结、拒绝复用、用量范围与故障证据，独立于pytest/CI且不访问凭据。

冻结源码的 `workbench/vendor` 仅绑定本仓库锁定vendor，绑定目标单独记入manifest并核验；不遍历/复制vendor依赖，也不接受其它越界资产链接。两臂原生runner在发送模型请求前做隔离DSH_HOME的模块导入检查，结果见 `native-preflight.json`；`--freeze-only`亦执行此零模型调用检查。原生模块导入失败属于评测基础设施错误，不计为模型质量失败。

2026-10-03 第一轮输出上限2048，12组双方各2轮完整记录，暴露大量 `DSH_TURN_INCOMPLETE`，保存在 `.workbench/chat-comparison-20261003`。收口补测统一8192，双方同参数，第1–6组完成双轮，第7–12组遇供应商 `insufficient_user_quota` 的403，后续轮次没有执行。汇总输出 `complete_multiturn_records`、`external_quota_failures` 和每组实际/计划轮数，不能把“有result.json”算成多轮完成。

两轮参数和实现阶段不同，不能合并为同一实验；2048的after不代表收口版本完整质量。补充评测需在同模型额度可用后使用新的输出目录，保留本次失败，不覆盖旧结果。单次结果可定位问题，不代表文学质量已普遍提升；详见[本轮验收记录](../../docs/工作台整体改造-验收记录-2026-10-03.md)与[OpenAI Agent evals](https://developers.openai.com/api/docs/guides/agent-evals)。

完成审计后的schema 3独立实验 `.workbench/chat-comparison-completion-v3-20261003` 已通过双方原生模块导入预检；双方第一组首轮仍收到明确额度不足，余下22个代码臂/用例任务及第一组第二轮未执行。当前源码完整多轮记录为0组。预检通过、历史12组记录和额度错误不能代替当前源码完整验收；额度恢复后仍需在新目录执行12组多轮对照及第12组的正确故障点。更早的 `chat-comparison-completion-20261003` vendor路径遗漏产生的启动错误单独保留，属于基础设施故障。
