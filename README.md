# AI 小说创作工作台

本地优先的中文小说创作工具：**Markdown 是唯一事实源**（章节正文例外，见下），SQLite 只做索引。生成管线的 Proposal 经收件箱应用；对话中的文件修改按该会话的权限设置执行，并保留差异、版本检查、快照和撤回。章节正文以 `章节/第NNNN章.txt` 存纯文本，标题/状态/合同ID/字数以 `chapters` 表为准。

- 后端：`workbench/backend/`（Python 3.11+ / FastAPI / SQLite）
- 前端：`workbench/frontend/`（React 18 + Vite + TypeScript）
- 技能源：`skills/<name>/SKILL.md`（同步产物在 `.dsh/skills/`，**禁止手工编辑**）
- 开发文档：`docs/AI小说创作工作台-开发文档.md`（产品/需求/方案）
- 技术文档：[项目技术文档](docs/AI小说创作工作台-项目技术文档.md)（实现级：分层、模块、接口清单、数据流、业务流程、**文档更新机制**）
- 其他：[近期对话修改与优化说明](docs/近期对话修改与优化说明-2026-09-24.md)；兼容矩阵：`docs/compat-matrix.md`；[快速启动](docs/快速启动.md)
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

端口按项目红线固定 **8790**；**禁用** 3080（dsh web）/ 8765 / 3456，
启动前会探测占用并明确报错，不会静默换端口。

## 3. 首次使用

1. **设置 → 模型供应商**：填 `baseURL` 与 API Key → 拉取模型列表 → 可批量添加同一供应商的多个模型并分别设置窗口/输出上限 → 设全局默认模型 → 健康检查 → 启用。
   保存或启用后会自动把 provider 路由同步到工作台自己的 `.workbench/dsh-home`；「投影到内置 dsh」可用于手动修复旧配置。
   Key 存在 `.workbench/secrets.json`（Windows 走 DPAPI 加密），不入 Git、不入导出包、不进日志。
2. **书架 → 新建项目**：填书名/题材/平台/主角/一句话设定，选择开书模板。
   工作台会生成 `projects/{书名}/` 完整工作区（备忘录 / 大纲 / 设定 / 状态 / 章节）。
3. **大纲页**：写灵感 → 追问关键问题 → 生成候选（每批 2-3 个**累计追加**，自动去重，可对单个候选**对话优化**）→ 锁定 → **冻结**（冻结后章节生产才放行；可随时**取消冻结**继续编辑）。
4. **编辑器**：新建章节 → 生成/冻结章节合同 → 跑「8 步循环」→ 到 **收件箱**看差异并应用。
   章节正文按 `第NNNN章.txt` 编号（文件内只有纯正文，标题/状态/字数在库）；输入普通名称时，它会保存为章节标题。
   对话区可附加参考文件并单独指定正文目标章节；写入权限默认自动执行（可切到「请求批准」严格模式），切换后当前任务立即生效、无需新开对话。
5. **审稿中心**：逐项审稿（待核实不算通过）、去AI味硬门 + 软审、质检进度表、质量债。
6. **拆书资产库**：导入对标样章 → 六维拆解（开篇钩子/情绪节拍/主角行动线/信息节奏/语言特征/可迁移手法）
   → 双时间线 + 事实卡（**强制章节依据**，无法取证标注「依据缺失」不补写）；同题材事实按需注入
   上下文第 4 级。落盘在 `projects/{书名}/拆书/{目标书名}/`。
7. **发布导出**（书架 → 发布导出）：按目标平台整理发布稿（标题格式/段落缩进/单章建议字数），
   并给出**提交前自检**（字数区间、章节状态、未解决质量债、简介与题材、无凭据泄漏）。
   每章字数区间默认 2000–4000 汉字（下限不足会被字数门拒收，超上限仅告警），可在「设置」改全局默认、在对话面板按本书覆盖。
8. **生图工坊**（全局区 `生图工坊`）：配置图片供应商 → 可选上传参考图（垫图/编辑）→ 生成 →
   画廊查看/收藏/改提示词/下载；产物落 `.workbench/images/`，可用「重建索引」扫盘恢复。

全局区还有 **仪表盘**（Token 用量与成本、最近任务）、**收件箱**（AI 产出的 diff 审阅与应用）、
**工作流**（节点编排、批量产章、断点续跑）与 **设置**（模型供应商/引擎路由/外观护眼/对话偏好/技能/Agent/规则/向量检索）。
项目内另有 **知识库与图谱**（`/project/:id/knowledge`：资料/实体图谱、语义检索问答、待审候选）与 **项目设置**（本书新会话默认权限、每章字数区间）。

## 4. 目录与数据

| 路径 | 内容 | 是否事实源 |
|---|---|---|
| `projects/{书名}/` | 正文（章节 `章节/第NNNN章.txt` 纯文本）、设定、状态、大纲（Markdown） | **是** |
| `.workbench/workbench.db` | 索引 / 任务 / 提案 / 审稿（可重建） | 否 |
| `.workbench/vector/books/<knowledge_key>/vectors.db` | 每本书独立的向量、图谱与检索任务库（可重建） | 否 |
| `.workbench/dsh-home/` | 工作台**独立** DSH_HOME | 否 |
| `.workbench/snapshots/`、`trash/` | 快照与回收站 | 否 |
| `skills/` `agents/` `rules/` `workflows/` `templates/` | 受管资产（版本化） | **是** |
| `.dsh/skills/` | 技能同步产物（禁止手工编辑） | 否 |

## 5. 隔离红线（务必遵守）

1. 工作台只用独立 `DSH_HOME = .workbench/dsh-home`，**永不读写**用户 `~/.dsh`；
2. 只调用 vendor 内置、版本锁定的 dsh（经 `workbench/backend/engine/dsh_paths.py`），
   代码库禁止裸 `dsh` 调用（`workbench/cli/check_no_bare_dsh.py` 静态拦截）；
3. 端口 8790，禁用 3080/8765/3456；
4. 工作台已登记的小说目录可按作者授权读写；`projects/` 中第三方参考目录只读，第三方资产引入前查 `docs/third-party-notices.md`。

## 6. 自检与测试

```powershell
# 当前可执行的应用测试与静态检查
.\.venv\Scripts\python.exe -m pytest workbench/backend/tests -q
# 大纲候选追加、去重、连续优化与冻结切换专项（临时项目 / 模拟模型）
.\.venv\Scripts\python.exe -m pytest workbench/backend/tests/test_outline_candidates.py workbench/backend/tests/test_outline_freeze.py workbench/backend/tests/test_content.py -q
npm --prefix workbench/frontend test
npm --prefix workbench/frontend run build
.\.venv\Scripts\python.exe workbench\cli\check_no_bare_dsh.py        # 裸调 dsh 静态扫描
.\.venv\Scripts\python.exe workbench\cli\check_ui_style.py           # UI 风格 / token 一致性
.\.venv\Scripts\python.exe workbench\cli\check_license_compliance.py # 第三方合规扫描
```

编辑器幽灵续写另有[浏览器回归模块](workbench/frontend/tests/ghostText.browser.acceptance.cjs)：先构建前端，再由浏览器自动化调用 `runGhostTextAcceptance(page)`（可传入 Tabbit 的 Page）。覆盖章首/中间选区点击追加章末、焦点与一次撤销、Tab/Esc、连续点击、组合输入，以及改文/切章后的迟到响应；所有 API 和生产 dist 资产均在测试页拦截，不使用真实书稿、后端或模型，不接入 `npm test`。

旧 `selfcheck.py`、`verify_isolation.py`、`final_report.py` 和部分 `workbench/tests/contract`、`workbench/tests/isolation` 会读取用户全局 `~/.dsh`，与当前“禁止读写”红线冲突。改造前不要运行这些脚本或整套测试；历史验收数字见[对话体验改造验收记录](docs/对话体验改造-验收记录.md)。

## 7. 卸载

当前 `workbench/cli/uninstall.py` 包含读取用户全局 `~/.dsh` 的旧校验流程，尚不符合现行隔离红线；改造前不要运行。卸载设计的保留范围仍是 `projects/` 中的正文与设定、`skills/`、`agents/`、`rules/`、`workflows/`、`templates/`、源码与文档。

## 8. 关键机制速查

- **双引擎**：语义任务（大纲/正文/审稿/润色/拆书）走 `dsh-headless`；
  结构化抽取、<500 字轻量改写、embedding 走 `direct-api`；dsh 不可用自动降级。
- **8 步循环**：LOAD → CHECK → CONTRACT → DRAFT → REVIEW → REVISE → UPDATE → DECIDE，
  每步 checkpoint，支持断点续跑；DRAFT 只写草稿区，落盘必须经收件箱。
- **上下文 9 级优先级**：用户材料 > 细纲/合同 > 前章末 500 字 > 设定+同题材拆书事实卡 > 角色状态 >
  伏笔台账 > 历史摘要 > 检索召回 > 本书认可写法样稿（可选，写作入口显式开启）；
  超预算自低向高裁剪，必读集（前章末 + 角色状态）保留并记录降级事件。
- **去AI味双门禁**：确定性脚本（破折号频次/对话密度/禁词/三连排比/规划记号泄漏）+
  模型软审（行级建议进收件箱，应用时校验原行是否仍匹配）。
- **本书知识库与图谱**：进入“知识库与图谱”可浏览材料、人物/事件关系和原文依据，
  使用关键词或 AI 检索旧剧情；向量与图谱按书隔离。默认 BGE 中文 ONNX 本地模型，
  首次需在知识库页准备模型并同步本书；不可用时显示原因并使用关键词检索，不使用哈希伪语义。
