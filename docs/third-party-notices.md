# 第三方合规登记（third-party notices）

本文件登记「AI 小说创作工作台」对外部参考项目的引入决策与已引入文件。
参考项目源码位于 `projects/`（被 `.gitignore` 忽略，仅作只读参考）。

**规则**：引入任何外部代码 / 文本前，必须先在此登记；
`❌ 禁用` 项目**只能借鉴思想，不得复制任何文件内容**（含代码、提示词、技能文本、规则词表）。
自动化校验见 `workbench/cli/check_license_compliance.py`。

## 一、引入决策总表

| 来源项目 | 许可证 | 可否引入 | 说明 |
|---|---|---|---|
| huahuakandaima__human-novel-writer | MIT | ✅ 可 | 保留版权声明与来源注释 |
| qin1473692580-ux__oh-story-claudecode | MIT | ✅ 可 | 保留版权声明与来源注释 |
| voocel__ainovel-cli | Apache-2.0 | ✅ 可 | **须保留 NOTICE** |
| Wooooooooood__ai-fiction-writer | MIT | ✅ 可 | 保留版权声明与来源注释 |
| xiaoshengxianjun__51mazi_retry | MIT | ✅ 可 | 保留版权声明与来源注释 |
| ponysb__91Writing | MIT | ✅ 可 | 保留版权声明与来源注释 |
| LanyXiaosheng__bookflow-skill | MIT | ✅ 可 | 保留版权声明与来源注释 |
| PenglongHuang__chinese-novelist-skill_retry | MIT | ✅ 可 | 保留版权声明与来源注释 |
| EthanYoQ__AI-Novel-Writer（仅 `plugins/dsh-ai-novel-writer/` 子目录） | MIT | ✅ 可 | **仅限该子目录**；保留版权声明与来源注释 |
| EthanYoQ__AI-Novel-Writer（主应用） | GPL | ❌ 仅思想 | 不得复制任何文件内容 |
| xiamuceer-j__MuMuAINovel | GPL | ❌ 仅思想 | 不得复制任何文件内容 |
| Mochocyang__QMAI | GPL | ❌ 仅思想 | 不得复制任何文件内容 |
| ExplosiveCoderflome__AI-Novel-Writing-Assistant | AGPL | ❌ 禁用 | 禁止以任何形式引入 |
| manhai934__novel-harness | CC BY-NC-SA 4.0 | ❌ 禁用 | **禁止移植其 `.harness/**`、`skills/**`、`memory/**`、`rag/**` 文本** |
| zy-zmc__tianming-skill | CC BY-NC-SA 4.0 | ❌ 禁用 | 禁止以任何形式引入 |
| ciel-oliver__analyze-hit-novel | 无 LICENSE | ❌ 禁用 | 无授权即默认保留全部权利 |
| inliver233__Ai-Novel | 无 LICENSE | ❌ 禁用 | 无授权即默认保留全部权利 |
| Jieice__ai-novel-writer | 无 LICENSE | ❌ 禁用 | 无授权即默认保留全部权利 |
| luciferlihaoyu__novel-skill-cards | 无 LICENSE | ❌ 禁用 | 无授权即默认保留全部权利 |
| qi531416__novel-humanize | 无 LICENSE | ❌ 禁用 | 无授权即默认保留全部权利 |

## 二、已引入文件登记

| 来源项目 | 许可证 | 引入文件 | 修改说明 |
|---|---|---|---|
| huahuakandaima__human-novel-writer | MIT | `projects/huahuakandaima__human-novel-writer/scripts/density_check.py` → `workbench/backend/gates/density_gate.py` | ① 顶部加来源声明注释块；② 检测逻辑与阈值**不变**；③ 阈值/词表抽为模块级常量并支持 `DensityConfig` 覆盖；④ 新增 `run(text) -> DensityResult` import 接口，保留原 CLI 输出格式与退出码 |
| huahuakandaima__human-novel-writer | MIT | `projects/huahuakandaima__human-novel-writer/scripts/check_prose.py` → `workbench/backend/gates/prose_gate.py` | ① 顶部加来源声明注释块；② 检测逻辑与阈值**不变**；③ 词表/阈值抽为模块级常量并支持 `ProseConfig` 覆盖；④ 新增 `run(text) -> ProseResult` import 接口，保留原 CLI 输出格式与退出码 |
| qin1473692580-ux__oh-story-claudecode | MIT | `projects/qin1473692580-ux__oh-story-claudecode/scripts/check-chinese-prose-contract.py`（中文正文契约）<br>`projects/qin1473692580-ux__oh-story-claudecode/skills/story-deslop/scripts/language_gate.js`（契约的语言门实现）→ `workbench/backend/gates/language_gate.py` | ① 顶部加来源声明注释块；② 检测逻辑与阈值**不变**（HTML 阻断、非叙事结构机械保护、外语 token 精确白名单）；③ 保护区正则与白名单边界规则抽为模块级常量；④ 新增 `run(text) -> LanguageResult` import 接口，保留 JSON / 文本报告与退出码语义 |

### 引入文件的许可证声明位置

三个门禁脚本的**文件头注释块**均保留：原项目名、许可证（MIT）、原文件路径、
引入日期、修改说明。修改后的衍生文件仍受 MIT 约束。

MIT 许可全文（版权声明 + 许可声明）随副本一并保留在
`workbench/backend/gates/THIRD_PARTY_LICENSES.txt`。

## 三、禁止事项速查

- 禁止把 `projects/` 下任何文件整体复制进 `workbench/` 或 `skills/`。
- 禁止移植 `manhai934__novel-harness` 的 `.harness/**`、`skills/**`、`memory/**`、`rag/**` 文本。
- 禁止引入 AGPL（`ExplosiveCoderflome__AI-Novel-Writing-Assistant`）任何内容。
- 无 LICENSE 项目一律视为「保留全部权利」，只能读、不能抄。
- Apache-2.0 项目引入时必须同时保留其 `NOTICE` 文件内容。
