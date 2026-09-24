# AI 小说创作工作台

本地优先的中文小说创作工具：**Markdown 是唯一事实源**，SQLite 只做索引，AI 产出必须经收件箱人工应用后才落盘。

- 后端：`workbench/backend/`（Python 3.11+ / FastAPI / SQLite）
- 前端：`workbench/frontend/`（React 18 + Vite + TypeScript）
- 技能源：`skills/<name>/SKILL.md`（同步产物在 `.dsh/skills/`，**禁止手工编辑**）
- 开发文档：`docs/AI小说创作工作台-开发文档.md`；兼容矩阵：`docs/compat-matrix.md`
- Spec / 任务 / 验收清单：`.trae/specs/build-workbench-m0-m1/`

## 1. 安装

```powershell
# 1) Python 依赖（建议虚拟环境）
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r workbench/backend/requirements.txt

# 2) 前端依赖与构建
npm --prefix workbench/frontend install
npm --prefix workbench/frontend run build

# 3) 内置 dsh（vendor 锁定安装 + 独立 DSH_HOME）——需要 Node ≥ 20
.\.venv\Scripts\python.exe workbench\cli\setup_dsh.py
```

`setup_dsh.py` 只做三件事：在 `workbench/vendor/dsh/` **本地**安装精确版本
`@deepseek-ai/dsh@0.1.0-rc.8`、初始化独立 `DSH_HOME = .workbench/dsh-home`、创建
profile `ai-novel-workbench`。它**不会**执行 `npm install -g`，**不会**执行 `dsh plugin`，
**不会**读写你的 `~/.dsh`。

## 2. 启动

```powershell
.\.venv\Scripts\python.exe -m workbench.backend.app
# 打开 http://127.0.0.1:8790
```

开发模式（热更新）：

```powershell
# 终端 A：后端
.\.venv\Scripts\python.exe -m workbench.backend.app
# 终端 B：前端（Vite dev server 代理 /api 到 8790）
npm --prefix workbench/frontend run dev
```

端口固定 **8790**（`WORKBENCH_PORT` 可覆盖）；**禁用** 3080（dsh web）/ 8765 / 3456，
启动前会探测占用并明确报错，不会静默换端口。

## 3. 首次使用

1. **设置 → 模型供应商**：填 `baseURL` 与 API Key → 拉取模型列表 → 健康检查 → 启用。
   保存或启用后会自动把 provider 路由同步到工作台自己的 `.workbench/dsh-home`；「投影到内置 dsh」可用于手动修复旧配置。
   Key 存在 `.workbench/secrets.json`（Windows 走 DPAPI 加密），不入 Git、不入导出包、不进日志。
2. **书架 → 新建项目**：填书名/题材/平台/主角/一句话设定，选择开书模板。
   工作台会生成 `projects/{书名}/` 完整工作区（备忘录 / 大纲 / 设定 / 状态 / 章节）。
3. **大纲页**：写灵感 → 追问关键问题 → 生成 2-3 个候选 → 锁定 → **冻结**（冻结后章节生产才放行）。
4. **编辑器**：新建章节 → 生成/冻结章节合同 → 跑「8 步循环」→ 到 **收件箱**看差异并应用。
5. **审稿中心**：逐项审稿（待核实不算通过）、去AI味硬门 + 软审、质检进度表、质量债。
6. **拆书资产库**：导入对标样章 → 六维拆解（开篇钩子/情绪节拍/主角行动线/信息节奏/语言特征/可迁移手法）
   → 双时间线 + 事实卡（**强制章节依据**，无法取证标注「依据缺失」不补写）；同题材事实按需注入
   上下文第 4 级。落盘在 `projects/{书名}/拆书/{目标书名}/`。
7. **发布导出**（书架 → 发布导出）：按目标平台整理发布稿（标题格式/段落缩进/单章建议字数），
   并给出**提交前自检**（字数区间、章节状态、未解决质量债、简介与题材、无凭据泄漏）。

## 4. 目录与数据

| 路径 | 内容 | 是否事实源 |
|---|---|---|
| `projects/{书名}/` | 正文、设定、状态、大纲（Markdown） | **是** |
| `.workbench/workbench.db` | 索引 / 任务 / 提案 / 审稿（可重建） | 否 |
| `.workbench/vector/vectors.db` | 向量库（本地嵌入，可重建） | 否 |
| `.workbench/dsh-home/` | 工作台**独立** DSH_HOME | 否 |
| `.workbench/snapshots/`、`trash/` | 快照与回收站 | 否 |
| `skills/` `agents/` `rules/` `workflows/` `templates/` | 受管资产（版本化） | **是** |
| `.dsh/skills/` | 技能同步产物（禁止手工编辑） | 否 |

## 5. 隔离红线（务必遵守）

1. 工作台只用独立 `DSH_HOME = .workbench/dsh-home`，**永不读写**用户 `~/.dsh`；
2. 只调用 vendor 内置、版本锁定的 dsh（经 `workbench/backend/engine/dsh_paths.py`），
   代码库禁止裸 `dsh` 调用（`workbench/cli/check_no_bare_dsh.py` 静态拦截）；
3. 端口 8790，禁用 3080/8765/3456；
4. `projects/` 为只读参考区（第三方素材），第三方资产引入前查 `docs/third-party-notices.md`。

## 6. 自检与测试

```powershell
# 全部单测 + 契约 + 隔离 + 冒烟 + 性能
.\.venv\Scripts\python.exe -m pytest workbench/backend/tests workbench/tests -q

# 单跑某一层
.\.venv\Scripts\python.exe -m pytest workbench/tests/contract -q     # 契约测试 ①-⑤
.\.venv\Scripts\python.exe -m pytest workbench/tests/isolation -q    # 隔离 I1-I6
.\.venv\Scripts\python.exe -m pytest workbench/tests/e2e -q          # 冒烟（含 dsh 禁用降级）
.\.venv\Scripts\python.exe -m pytest workbench/tests/perf -q         # 性能基准

# 专项自检
.\.venv\Scripts\python.exe workbench\cli\check_no_bare_dsh.py        # 裸调 dsh 静态扫描
.\.venv\Scripts\python.exe workbench\cli\check_ui_style.py           # UI 风格 / token 一致性
.\.venv\Scripts\python.exe workbench\cli\check_license_compliance.py # 第三方合规扫描
.\.venv\Scripts\python.exe workbench\cli\selfcheck.py --verify       # 用户 ~/.dsh 基线比对
.\.venv\Scripts\python.exe workbench\cli\final_report.py             # 终验报告（写 .workbench/reports/）
```

## 7. 卸载

```powershell
.\.venv\Scripts\python.exe workbench\cli\uninstall.py --dry-run   # 先看清单
.\.venv\Scripts\python.exe workbench\cli\uninstall.py --yes       # 实际删除
```

删除：`workbench/vendor/dsh/`、`.workbench/`、`workbench/frontend/dist/`、`.pytest_cache/`。
保留：`projects/`（正文与设定）、`skills/` `agents/` `rules/` `workflows/` `templates/`、源码与文档。
用户全局 npm 与 `~/.dsh` 全程不被修改（卸载脚本只做只读比对）。

## 8. 关键机制速查

- **双引擎**：语义任务（大纲/正文/审稿/润色/拆书）走 `dsh-headless`；
  结构化抽取、<500 字轻量改写、embedding 走 `direct-api`；dsh 不可用自动降级。
- **8 步循环**：LOAD → CHECK → CONTRACT → DRAFT → REVIEW → REVISE → UPDATE → DECIDE，
  每步 checkpoint，支持断点续跑；DRAFT 只写草稿区，落盘必须经收件箱。
- **上下文 8 级优先级**：用户材料 > 细纲/合同 > 前章末 500 字 > 设定 > 角色状态 >
  伏笔台账 > 历史摘要 > 检索召回；超预算自低向高裁剪，必读集保留并记录降级事件。
- **去AI味双门禁**：确定性脚本（破折号频次/对话密度/禁词/三连排比/规划记号泄漏）+
  模型软审（行级建议进收件箱，应用时校验原行是否仍匹配）。
- **本地向量检索**：默认零下载的 `local-hashing` 嵌入（离线、可重建），
  可选 ONNX 本地模型；不可用时自动降级 FTS。
