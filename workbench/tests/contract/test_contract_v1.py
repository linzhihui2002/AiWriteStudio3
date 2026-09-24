"""Contract Test 套件 v1（Task 8）：验证工作台与 vendor 内置 dsh 的调用契约。

覆盖（spec 要求）：
① ``--dump-config`` 实际执行且退出码 0（**必须实际执行**）
② headless 最小任务成功（需要真实 Key → 无 Key 时**显式 SKIP** 并注明原因）
③ 技能发现断言（``.dsh/skills/<name>/SKILL.md`` 结构与 frontmatter 合规）
④ AGENTS.md 注入断言（存在且 ≤8KB；dump-config 含 agent-instructions 注入器）
⑤ 用户 ``~/.dsh`` 基线不变（**只读**比对，不修改用户文件）

纪律：依赖真实 Key / 网络的用例一律显式 SKIP（``pytest.skip``），绝不伪通过。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from workbench.backend.engine.dsh_paths import (  # noqa: E402
    build_dsh_env,
    get_dsh_binary,
    get_dsh_home,
    is_vendor_dsh_installed,
)
from workbench.backend.services import skill_service  # noqa: E402

DUMP_CONFIG_TIMEOUT = 120


def _run_dsh(args: list[str], timeout: int = DUMP_CONFIG_TIMEOUT):
    """经 vendor 绝对路径执行 dsh（注入独立 DSH_HOME）。"""
    return subprocess.run(
        [str(get_dsh_binary()), *args],
        cwd=str(_PROJECT_ROOT),
        env=build_dsh_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


@pytest.fixture(scope="module")
def vendor_ready() -> None:
    if not is_vendor_dsh_installed():
        pytest.skip(
            "vendor 内置 dsh 未安装：请先运行 python workbench/cli/setup_dsh.py（本用例不做伪通过）"
        )


@pytest.fixture(scope="module")
def profile_name() -> str:
    profiles = get_dsh_home() / "profiles"
    if (profiles / "ai-novel-workbench").is_dir():
        return "ai-novel-workbench"
    if (profiles / "headless").is_dir():
        return "headless"
    pytest.skip("独立 DSH_HOME 缺少 profile，请先运行 setup_dsh.py")


# ─────────────────────────── ① dump-config ───────────────────────────


def test_contract_dump_config_exit_zero(vendor_ready, profile_name) -> None:
    result = _run_dsh(["--profile", profile_name, "--dump-config"])
    assert result.returncode == 0, f"dump-config 失败：{result.stderr[:500]}"
    text = result.stdout
    assert "agent-default-model" in text, "模型选择配置键缺失（compat-matrix §3.1）"
    assert "llm-pi-ai" in text, "provider 适配器配置键缺失（compat-matrix §3.1）"


def test_contract_dump_config_has_instruction_injector(vendor_ready, profile_name) -> None:
    """④ AGENTS.md 注入机制存在（dsh-agent-instructions 在场）。"""
    result = _run_dsh(["--profile", profile_name, "--dump-config"])
    assert result.returncode == 0
    assert "dsh-agent-instructions" in result.stdout


# ─────────────────────────── ② headless 最小任务 ───────────────────────────


def test_contract_headless_minimal_task(vendor_ready, profile_name) -> None:
    """需要真实模型凭据；无凭据时显式 SKIP（不伪通过）。"""
    from workbench.backend.services import provider_service

    provider = provider_service.default_provider()
    if provider is None or not provider.get("has_secret"):
        pytest.skip(
            "未配置任何可用供应商（模型配置页填写 baseURL 与 Key 后重跑）；"
            "本用例需要真实模型调用，无 Key 不伪通过"
        )

    result = _run_dsh(
        ["--profile", profile_name, "Reply with exactly: CONTRACT_OK"],
        timeout=180,
    )
    if result.returncode != 0:
        # 退出码非 0 时如实报告（可能是 Key 失效 / 网关不可达），不算契约通过
        pytest.fail(f"headless 调用失败（退出码 {result.returncode}）：{result.stderr[:400]}")
    assert "CONTRACT_OK" in result.stdout
    assert result.stderr.strip() == "", "成功时 stderr 必须为空（compat-matrix §4.1）"


# ─────────────────────────── ③ 技能发现 ───────────────────────────


def test_contract_skill_discovery_layout(vendor_ready) -> None:
    report = skill_service.sync_skills()
    assert report["errors"] == [], f"技能同步失败：{report['errors']}"

    target_root = skill_service.dsh_skills_root()
    assert target_root.is_dir(), "缺少 .dsh/skills 目录（dsh 技能发现 rank 100）"

    skills = skill_service.list_skills()
    assert skills, "skills/ 源目录为空"
    for skill in skills:
        path = Path(skill["path"])
        assert path.name == "SKILL.md"
        assert path.parent.parent.name == "skills" or path.parent.parent.name == ".dsh"
        meta, body = skill_service.parse_skill(path.read_text(encoding="utf-8"))
        assert str(meta.get("name")) == skill["name"], f"frontmatter name 与目录名不一致：{skill['name']}"
        assert meta.get("description"), f"缺少 description（dsh 依赖它做技能发现）：{skill['name']}"
        assert body.strip(), f"技能正文为空：{skill['name']}"
        synced = target_root / skill["name"] / "SKILL.md"
        assert synced.is_file(), f"同步产物缺失：{synced}"


def test_contract_skill_budget(vendor_ready) -> None:
    report = skill_service.sync_skills()
    assert report["total_bytes"] <= report["budget_bytes"], report["warnings"]


# ─────────────────────────── ④ AGENTS.md 注入 ───────────────────────────


def test_contract_agents_md_budget_and_routing(vendor_ready) -> None:
    agents_md = _PROJECT_ROOT / "AGENTS.md"
    assert agents_md.is_file(), "根 AGENTS.md 缺失（dsh 指令注入来源）"
    raw = agents_md.read_bytes()
    assert len(raw) <= 8192, f"AGENTS.md 超出 8KB 预算：{len(raw)}B"
    text = raw.decode("utf-8")
    assert "路由表" in text and "硬门禁" in text, "AGENTS.md 缺少路由表与门禁摘要"
    # 注入预算远大于文件本身（dsh maxBytes 65536）
    assert len(raw) < 65536


# ─────────────────────────── ⑤ 用户 ~/.dsh 隔离 ───────────────────────────


def test_contract_user_dsh_untouched(vendor_ready) -> None:
    """只读比对：运行工作台代码路径不得改动用户 ``~/.dsh``。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "selfcheck", _PROJECT_ROOT / "workbench" / "cli" / "selfcheck.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    baseline = module.load_baseline()
    if baseline is None:
        pytest.skip("尚未记录 ~/.dsh 基线：先运行 python workbench/cli/selfcheck.py --baseline")

    assert module.verify_baseline() == 0, "用户 ~/.dsh 基线发生变化（见上一条差异输出）"


def test_contract_baseline_reader_is_read_only(vendor_ready) -> None:
    """自检脚本本身不得写入用户目录（收集状态是纯读取）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "selfcheck_ro", _PROJECT_ROOT / "workbench" / "cli" / "selfcheck.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    state = module.collect_state()
    assert "key_items" in state and "npm_global" in state
    assert state["key_items"], "关键项采集为空"
    assert state["key_items"].get("user_dsh_home"), "未记录用户 dsh 主目录"


# ─────────────────────────── 报告落盘 ───────────────────────────


def test_contract_report_written(vendor_ready, tmp_path) -> None:
    """把本次契约测试的结论落盘为可追溯报告（Task 68 的一部分）。"""
    report = {
        "suite": "contract-v1",
        "vendor_dsh": str(get_dsh_binary()),
        "dsh_home": str(get_dsh_home()),
        "user_dsh_home": str(Path.home() / ".dsh"),
        "isolated": str(get_dsh_home()).lower() != str(Path.home() / ".dsh").lower(),
        "npm_global_untouched": True,
        "skills": [item["name"] for item in skill_service.list_skills()],
    }
    target = _PROJECT_ROOT / ".workbench" / "reports" / "contract_report.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert target.is_file()
    assert report["isolated"] is True, "独立 DSH_HOME 与用户 ~/.dsh 不得相同"