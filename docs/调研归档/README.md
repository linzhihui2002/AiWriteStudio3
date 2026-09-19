# 调研归档

本目录存放本项目所有调研产出的原始事实与来源。依据《调研归档长期规则》：

- 凡联网 / 子代理（Explore 等）调研产出，必须落盘为 md 文档归档于此，不能只留在对话里。
- 归档存**原始事实与来源**；PRD / 开发文档存**提炼后的产品决策**。归档是决策的依据。
- 文件命名：`调研-YYYY-MM-DD-主题.md`；每个文件开头必须有「调研元信息」表。
- 正文用结构化要点，标注「是否实测」与证据等级，不把厂商宣传 / 社区观点当成果事实。

## 索引

| 文件 | 主题 | 调研方式 | 用途 |
|---|---|---|---|
| [调研-2026-09-19-DeepSeekHarness机制与本机安装现状.md](./调研-2026-09-19-DeepSeekHarness机制与本机安装现状.md) | dsh 框架机制（源码级）+ 本机安装实测 | 本机静态检查 + 安装包源码审读 | 开发文档第 2、3、6 章的冲突分析与集成方案依据 |
| [调研-2026-09-19-AI小说开源项目横向调研.md](./调研-2026-09-19-AI小说开源项目横向调研.md) | 18 个本地拉取的开源小说项目横向调研 | 子代理（Explore）对本地仓库 README 与目录结构审读 | 开发文档第 4、5、7、8 章的设计模式依据 |

## 关联基线文档（非本目录，但为重要依据）

- AiWriteStudio2 PRD 完整基线：`d:\desktop\AiWriteStudio2\reports\PRD-AI-Novel-Workbench.md`（FR-* 功能编号体系、FR-MODEL-000 系列隔离硬性要求）
- AiWriteStudio2 PRD 摘要：`d:\desktop\AiWriteStudio2\docs\project-baseline\01-PRD.md`（6.1 DSH 共存硬性要求）
- 本项目模型 API 记录：`d:\desktop\AiWriteStudio3\模型API.md`
- 本项目 GitHub 参考项目链接清单：`d:\desktop\AiWriteStudio3\githubAI写小说项目链接.md`
