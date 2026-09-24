"""Native vendor chat contract tests; no external API key or paid model calls."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
import yaml

from workbench.backend.engine import dsh_chat_profile, dsh_paths
from workbench.backend.engine.dsh_chat import DshChatRuntime, RunControl
from workbench.backend.engine.dsh_engine import DshEngine


MOCK_PLUGIN = r'''
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
const req = createRequire(VENDOR_MANIFEST);
const { LlmAdapter, CallId, freezeMessage, MessageId } = await import(pathToFileURL(req.resolve('@deepseek-ai/dsh-llm')).href);
export const name = 'workbench-chat-contract-model';
export const inject = ['llm'];
export function apply(ctx) {
  class Mock extends LlmAdapter {
    async resolveModel(provider, model) {
      return {provider, id:model, name:model, context:{contextWindow:100000}, defaultMaxTokens:10000};
    }
    async *stream(options) {
      const textOf = message => message.content.filter(b => b.type === 'text').map(b => b.text).join('');
      const humans = options.messages.filter(m => m.source.kind === 'user');
      const last = textOf(humans.at(-1));
      if (last === 'wait') {
        yield {type:'block-start',index:0,blockType:'text'};
        yield {type:'text-delta',index:0,text:'working'};
        await new Promise((resolve, reject) => {
          const timer = setTimeout(resolve, 15000);
          options.signal.addEventListener('abort', () => {clearTimeout(timer); reject(new Error('cancelled'));}, {once:true});
        });
      }
      const toolResult = options.messages.at(-1)?.source.kind === 'tool';
      if ((last === 'tool' || last === 'unsafe' || last === 'bad-args' || last === 'whitespace-tool') && !toolResult) {
        const name = last === 'unsafe' ? 'write' : 'save_novel';
        const args = last === 'bad-args' ? {path:42} : {path:'设定/人物.md',content:'林川'};
        const id = CallId('mock-call-1');
        if (last === 'tool') {
          yield {type:'block-start',index:0,blockType:'text'};
          yield {type:'text-delta',index:0,text:'正在保存。'};
          yield {type:'block-end',index:0,block:{type:'text',text:'正在保存。'}};
        }
        if (last === 'whitespace-tool') {
          yield {type:'block-start',index:0,blockType:'text'};
          yield {type:'text-delta',index:0,text:'\n\n'};
          yield {type:'block-end',index:0,block:{type:'text',text:'\n\n'}};
        }
        const index = ['tool','whitespace-tool'].includes(last) ? 1 : 0;
        yield {type:'block-start',index,blockType:'tool-call'};
        yield {type:'tool-call-delta',index,id,name,argumentsDelta:JSON.stringify(args)};
        yield {type:'block-end',index,block:{type:'tool-call',id,name,arguments:JSON.stringify(args)}};
        yield {type:'finish',reason:{kind:'tool-calls'}};
        return;
      }
      let answer = 'OK';
      if (last === 'recall') answer = humans.map(textOf).find(t => t.startsWith('remember:'))?.slice(9) || 'MISSING';
      if (last === 'catalog') answer = (options.tools || []).map(t => t.name).sort().join(',');
      if (toolResult) answer = JSON.stringify(options.messages.at(-1).content);
      if (last === 'model') answer = options.model;
      if (last === 'context') answer = options.messages.map(textOf).join('|');
      if (last === 'history-count') answer = String(options.messages.filter(m =>
        m.source.kind === 'plugin' && m.source.form === 'recall' && textOf(m).includes('李长歌')).length);
      if (last === 'recovered-history') answer = JSON.stringify({
        recalls: options.messages.filter(m => m.source.kind === 'plugin' && m.source.form === 'recall' && textOf(m).includes('tool')).length,
        oldLiveRequests: humans.filter(m => textOf(m) === 'tool').length,
      });
      yield {type:'block-start',index:0,blockType:'text'};
      for (const text of [answer.slice(0,2), answer.slice(2)].filter(Boolean)) yield {type:'text-delta',index:0,text};
      yield {type:'block-end',index:0,block:{type:'text',text:answer}};
      yield {type:'usage',usage:{inputTokens:17,outputTokens:4}};
      yield {type:'finish',reason:{kind:'stop'}};
    }
  }
  ctx.llm.registerAdapter(['workbench-contract'], new Mock());
}
'''


@pytest.fixture()
def native_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    if not dsh_paths.is_vendor_dsh_installed() or not dsh_paths.get_node_binary():
        pytest.skip("pinned vendor dsh / Node is not installed")
    monkeypatch.setattr(dsh_paths, "DSH_HOME_DIR", tmp_path / ".workbench" / "dsh-home")
    monkeypatch.setattr(dsh_chat_profile, "get_project_root", lambda: tmp_path)
    project = tmp_path / "小说"
    project.mkdir()
    manifest = (dsh_paths.get_vendor_dsh_dir() / "package.json").as_uri()
    plugin = tmp_path / "mock.mjs"
    plugin.write_text(MOCK_PLUGIN.replace("VENDOR_MANIFEST", json.dumps(manifest)), encoding="utf-8")
    overlay = tmp_path / "test-model.yml"
    overlay.write_text(yaml.safe_dump([
        {"id": "llm-pi-ai", "disabled": True},
        {"id": "llm-deepseek", "disabled": True},
        {"insert": [{"id": "contract-model", "name": plugin.as_uri()}]},
    ]), encoding="utf-8")
    runtime = DshChatRuntime(timeout_seconds=35)
    command = runtime._command
    monkeypatch.setattr(runtime, "_command", lambda: [*command(), "--patch", str(overlay)])
    try:
        yield runtime, project
    finally:
        runtime.close_all()


def run(runtime: DshChatRuntime, project: Path, text: str, *, session="chat-contract", resume=False,
        tools=None, execute=None, cancel=None, model="mock", context=""):
    return list(runtime.stream(
        session_id=session, resume=resume, project_root=str(project), user_text=text,
        system="小说助手测试。", context=context, provider="workbench-contract", model=model,
        tool_specs=tools or [], execute_tool=execute or (lambda name, arguments, call_id: {"ok": True}),
        should_cancel=cancel,
    ))


def assert_done(events):
    assert events[-1]["type"] == "done", events
    assert sum(event["type"] in {"done", "error", "cancelled"} for event in events) == 1
    return events[-1]["text"]


def test_native_two_turns_and_cold_resume(native_runtime):
    runtime, project = native_runtime
    first = run(runtime, project, "remember:霜城-731")
    assert first[0]["type"] == "session_ready", first
    assert_done(first)
    assert any(event["type"] == "delta" for event in first)
    assert next(event["usage"] for event in first if event["type"] == "usage")["inputTokens"] == 17
    assert assert_done(run(runtime, project, "recall", resume=True)) == "霜城-731"
    runtime.close("chat-contract")
    restored = run(runtime, project, "recall", resume=True)
    assert assert_done(restored) == "霜城-731"
    assert restored[0]["resumed"] is True
    assert list((dsh_paths.get_dsh_home() / "chat-sessions").rglob("session.jsonl.zstd"))


def test_native_worker_reloads_credentials_and_retains_history(native_runtime):
    runtime, project = native_runtime
    assert_done(run(runtime, project, "remember:原生历史"))
    old_worker = runtime._workers["chat-contract"]
    credentials = dsh_paths.get_dsh_home() / ".credentials.yaml"
    credentials.write_text("WB_TEST_API_KEY: replacement\n", encoding="utf-8")

    assert assert_done(run(runtime, project, "recall", resume=True)) == "原生历史"
    assert runtime._workers["chat-contract"] is not old_worker
    assert old_worker.proc.poll() is not None


SAVE_TOOL = {"type": "function", "function": {
    "name": "save_novel", "description": "保存本书文件",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                   "required": ["path", "content"], "additionalProperties": False},
}}


def test_native_tools_bridge_and_allowlist(native_runtime):
    runtime, project = native_runtime
    calls = []

    def execute(name, arguments, call_id):
        calls.append((name, arguments, call_id))
        return {"ok": True, "summary": "已保存人物设定", "change_id": "change-1"}

    catalog = run(runtime, project, "catalog", tools=[SAVE_TOOL])
    assert assert_done(catalog) == "save_novel,skill"
    result = run(runtime, project, "tool", resume=True, tools=[SAVE_TOOL], execute=execute)
    assert "".join(event["text"] for event in result if event["type"] == "delta") == assert_done(result)
    assert calls == [("save_novel", {"path": "设定/人物.md", "content": "林川"}, "mock-call-1")]
    assert next(event["result"] for event in result if event["type"] == "tool_result")["change_id"] == "change-1"
    calls.clear()
    invalid = run(runtime, project, "bad-args", resume=True, tools=[SAVE_TOOL], execute=execute)
    assert_done(invalid)
    assert calls == []
    assert any(event["type"] == "tool_result" and event["result"]["ok"] is False for event in invalid)
    denied = run(runtime, project, "unsafe", resume=True, tools=[SAVE_TOOL], execute=execute)
    assert_done(denied)
    assert calls == []
    assert not (project / "设定" / "人物.md").exists()


def test_native_cancel_and_resume(native_runtime):
    runtime, project = native_runtime
    cancellation = threading.Event()
    events = []
    for event in runtime.stream(session_id="chat-cancel", resume=False, project_root=str(project),
                                user_text="wait", provider="workbench-contract", model="mock",
                                tool_specs=[], execute_tool=lambda *args: {}, should_cancel=cancellation.is_set):
        events.append(event)
        if event["type"] == "delta":
            cancellation.set()
    assert events[-1]["type"] == "cancelled", events
    assert sum(event["type"] in {"done", "error", "cancelled"} for event in events) == 1
    phases = [event for event in events if event["type"] == "activity"]
    assert phases[-1]["status"] == "error" and phases[-1]["label"] == "模型处理已停止"
    runtime.close("chat-cancel")
    assert assert_done(run(runtime, project, "model", session="chat-cancel", resume=True, model="changed-model")) == "changed-model"


def test_guard_blocks_a_registered_native_tool(native_runtime):
    runtime, project = native_runtime
    plugin = project.parent / "mock.mjs"
    source = plugin.read_text(encoding="utf-8")
    source = source.replace("export const inject = ['llm'];", "export const inject = ['llm', 'tools'];")
    source = source.replace("ctx.llm.registerAdapter(['workbench-contract'], new Mock());", """
      ctx.tools.register({name:'write', description:'Test forbidden native entry',
        parameters:{type:'object',additionalProperties:true},
        output:{schema:{},render:(_args,value)=>[{type:'text',text:JSON.stringify(value)}]},
        execute:async()=>({marker:'GUARD_BYPASSED'})});
      ctx.llm.registerAdapter(['workbench-contract'], new Mock());
    """)
    plugin.write_text(source, encoding="utf-8")
    events = run(runtime, project, "unsafe")
    result = next(event["result"] for event in events if event["type"] == "tool_result")
    assert result["ok"] is False, events
    assert "工作台授权" in result["text"]
    assert "GUARD_BYPASSED" not in assert_done(events)


def test_cancel_before_tool_execution_never_invokes_callback(native_runtime):
    runtime, project = native_runtime
    cancellation = threading.Event()
    calls = []
    events = []
    for event in runtime.stream(session_id="chat-before-tool", resume=False, project_root=str(project),
                                user_text="tool", provider="workbench-contract", model="mock",
                                tool_specs=[SAVE_TOOL], execute_tool=lambda *args: calls.append(args),
                                should_cancel=cancellation.is_set):
        events.append(event)
        if event["type"] == "tool_call":
            cancellation.set()
    assert events[-1]["type"] == "cancelled", events
    assert calls == []


def test_timeout_is_one_terminal_event_and_session_still_resumes(native_runtime):
    runtime, project = native_runtime
    assert_done(run(runtime, project, "remember:原生历史"))
    runtime.timeout_seconds = 0.5
    events = run(runtime, project, "wait", resume=True)
    assert events[-1]["type"] == "error" and events[-1]["code"] == "TIMEOUT", events
    assert sum(event["type"] in {"done", "error", "cancelled"} for event in events) == 1
    phases = [event for event in events if event["type"] == "activity"]
    assert phases[-1]["status"] == "error"
    runtime.timeout_seconds = 35
    runtime.close("chat-contract")
    assert assert_done(run(runtime, project, "recall", resume=True)) == "原生历史"


def test_cold_resume_rejects_a_different_novel(native_runtime):
    runtime, project = native_runtime
    assert_done(run(runtime, project, "remember:本书数据"))
    runtime.close("chat-contract")
    other = project.parent / "另一部小说"
    other.mkdir()
    events = run(runtime, other, "recall", resume=True)
    assert events[-1]["type"] == "error", events
    assert "另一部小说" in events[-1]["message"]
    assert not any(event["type"] == "delta" for event in events)


def test_resume_missing_does_not_silently_reset(native_runtime):
    runtime, project = native_runtime
    events = run(runtime, project, "hello", session="missing", resume=True)
    assert events[-1]["type"] == "error", events
    assert not any(event["type"] == "session_ready" for event in events)


def test_profile_limits_skills_and_tools(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(dsh_paths, "DSH_HOME_DIR", tmp_path / "dsh-home")
    monkeypatch.setattr(dsh_chat_profile, "get_project_root", lambda: tmp_path)
    overlay = dsh_chat_profile.ensure_chat_profile()
    rows = yaml.safe_load(overlay.read_text(encoding="utf-8"))
    by_id = {row["id"]: row for row in rows if "id" in row}
    skills = by_id["skill-filesystem"]["config"]
    assert skills["includeDefaultRoots"] is False
    assert skills["customSkillDirs"] == [str(tmp_path / ".dsh" / "skills")]
    for name in ("tool-fs", "tool-str-replace-editor", "tool-bash", "tool-pwsh", "tool-subagent", "agent-instructions"):
        assert by_id[name]["disabled"] is True
    assert by_id["tools"]["config"]["mode"] == "native"
    assert Path(by_id["session-persistence-jsonl"]["config"]["root"]).parent == dsh_paths.get_dsh_home()


def test_prune_never_touches_chat_or_protected_sessions(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(dsh_paths, "DSH_HOME_DIR", tmp_path)
    legacy = tmp_path / "sessions" / "project" / "old" / "session.jsonl"
    protected = tmp_path / "sessions" / "project" / "protected" / "session.jsonl"
    chat = tmp_path / "chat-sessions" / "project" / "chat" / "session.jsonl"
    for path in (legacy, protected, chat):
        path.parent.mkdir(parents=True)
        path.write_text("test", encoding="utf-8")
    assert DshEngine().prune_sessions(keep=0, protected_session_ids={"protected"}) == 1
    assert protected.exists() and chat.exists() and not legacy.exists()


def test_history_delivery_ack_survives_cold_resume_without_duplicate(native_runtime):
    runtime, project = native_runtime
    history = [{"id": 22, "role": "user", "content": "主角名字用李长歌", "status": "failed"}]
    def send(mid, resume):
        return list(runtime.stream(session_id="delivery", resume=resume, project_root=str(project),
            user_text="history-count", message_id=mid, history=history,
            provider="workbench-contract", model="mock", tool_specs=[], execute_tool=lambda *args: {}))
    first = send(23, False)
    assert assert_done(first) == "1"
    ack = next(e for e in first if e["type"] == "delivery_ack")
    assert ack["message_ids"] == [22, 23]
    runtime.close("delivery")
    # Simulates loss of the acknowledgement: replayed historical ID is not injected twice.
    assert assert_done(send(24, True)) == "1"


def test_human_wait_does_not_consume_model_timeout(native_runtime):
    runtime, project = native_runtime
    assert_done(run(runtime, project, "warmup"))
    runtime.timeout_seconds = .5
    control = RunControl()
    def execute(*args):
        control.begin_wait()
        try:
            time.sleep(.8)
        finally:
            control.end_wait()
        return {"ok": True, "summary": "作者已批准"}
    events = list(runtime.stream(session_id="chat-contract", resume=True, project_root=str(project),
        user_text="tool", provider="workbench-contract", model="mock", tool_specs=[SAVE_TOOL],
        execute_tool=execute, wait_control=control))
    assert "作者已批准" in assert_done(events)
    assert control.paused_seconds() >= .8


def test_whitespace_tool_steps_do_not_insert_blank_paragraphs(native_runtime):
    runtime, project = native_runtime
    events = run(runtime, project, "whitespace-tool", tools=[SAVE_TOOL])
    text = assert_done(events)
    assert text and not text.startswith("\n")
    assert "".join(e["text"] for e in events if e["type"] == "delta") == text
    activities = [e for e in events if e["type"] == "activity"]
    assert any(e["status"] == "running" for e in activities)
    assert any(e["status"] == "done" and "elapsed_ms" in e for e in activities)


@pytest.mark.parametrize("breakpoint", ["queued", "claimed"])
def test_cold_resume_recalls_interrupted_inbox_without_executing_old_request(native_runtime, breakpoint):
    runtime, project = native_runtime
    plugin = project.parent / "mock.mjs"
    source = plugin.read_text(encoding="utf-8")
    setup = """
      ctx.on('agent/session-start', ({agent, source}) => {
        if (source === 'startup') agent.inbox.splice('next-turn', Infinity, 0, [freezeMessage({
          id: MessageId(`workbench-message:${String(agent.id)}:71`), role:'user', source:{kind:'user'},
          content:[{type:'text',text:'tool'}],
        })]);
      });
    """ if breakpoint == "queued" else """
      ctx.on('agent/pre-step', async ({messages}, next) => {
        if (messages.some(m => m.source.kind === 'user' && m.content.some(b => b.type === 'text' && b.text === 'tool')))
          await new Promise(() => {});
        return next();
      });
    """
    source = source.replace("ctx.llm.registerAdapter(['workbench-contract'], new Mock());",
                            setup + "ctx.llm.registerAdapter(['workbench-contract'], new Mock());")
    plugin.write_text(source, encoding="utf-8")
    calls = []
    events = []
    stop_event = "session_ready" if breakpoint == "queued" else "delivery_ack"
    for event in runtime.stream(session_id="crash-inbox", resume=False, project_root=str(project),
            user_text="tool", message_id=71, provider="workbench-contract", model="mock",
            tool_specs=[SAVE_TOOL], execute_tool=lambda *args: calls.append(args)):
        events.append(event)
        if event["type"] == stop_event:
            # Abrupt exit, without the graceful disposal that clears the inbox.
            DshEngine._kill_tree(runtime._workers["crash-inbox"].proc)
    assert any(event["type"] == stop_event for event in events)
    assert events[-1]["type"] == "error" and calls == []
    runtime.close("crash-inbox")
    resumed = run(runtime, project, "recovered-history", session="crash-inbox", resume=True,
                  tools=[SAVE_TOOL], execute=lambda *args: calls.append(args))
    assert json.loads(assert_done(resumed)) == {"recalls": 1, "oldLiveRequests": 0}
    assert calls == []
    # A second cold recovery must not duplicate the materialized historical input.
    runtime.close("crash-inbox")
    again = run(runtime, project, "recovered-history", session="crash-inbox", resume=True,
                tools=[SAVE_TOOL], execute=lambda *args: calls.append(args))
    assert json.loads(assert_done(again)) == {"recalls": 1, "oldLiveRequests": 0}
    assert calls == []
