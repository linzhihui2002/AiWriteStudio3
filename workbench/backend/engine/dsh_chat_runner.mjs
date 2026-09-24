/** Workbench-owned stdio surface for the pinned dsh Agent API. No HTTP server. */
import { createRequire } from "node:module";
import { realpathSync } from "node:fs";
import { dirname, relative, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { createInterface } from "node:readline";

// Resolve only against our locked installation, never a globally installed dsh.
const vendorManifest = new URL("../../vendor/dsh/package.json", import.meta.url);
const vendorModules = realpathSync(resolve(dirname(fileURLToPath(vendorManifest)), "node_modules"));
const vendorRequire = createRequire(vendorManifest);
async function vendorImport(name) {
  const path = realpathSync(vendorRequire.resolve(name));
  const within = relative(vendorModules, path);
  if (within.startsWith("..") || within.includes(":")) throw new Error("DSH_VENDOR_ESCAPE");
  return import(pathToFileURL(path).href);
}
const [{ installModelSelection }, { createUserMessage, freezeMessage, MessageId }, { SessionId }, { validateJsonSchemaValue }, { assertEntriesActivated }] = await Promise.all([
  vendorImport("@deepseek-ai/dsh-agent"),
  vendorImport("@deepseek-ai/dsh-llm"),
  vendorImport("@deepseek-ai/dsh-session"),
  vendorImport("@deepseek-ai/dsh-tools"),
  vendorImport("@deepseek-ai/dsh-app-boot"),
]);

export const name = "workbench-chat-runner";
export const inject = ["agents", "sessions", "sessionPersistence", "tools", "systemPrompt"];
const MAX_INPUT_BYTES = 8 * 1024 * 1024;
const RESERVED_TOOLS = new Set(["skill", "run_code", "read", "write", "edit", "bash", "pwsh", "shell"]);

export function apply(ctx) {
  const write = (event) => process.stdout.write(JSON.stringify({ protocol: 1, ...event }) + "\n");
  const input = createInterface({ input: process.stdin, crlfDelay: Infinity });
  const pendingTools = new Map();
  let handle;
  let initialization;
  let active;
  let activeRun;
  let closing = false;
  let cancelRequested = false;
  let systemText = "";
  let contextText = "";
  let toolDisposers = [];
  let allowedTools = new Set(["skill"]);
  const selection = { current: undefined, assembled: undefined };
  const calls = new Map();
  const canonicalResults = new Map();
  const answeredQuestionCalls = new Set();
  const streamedSteps = new Map();
  const stepWhitespace = new Map();
  const phaseStarts = new Map();
  const phaseLabels = new Map();
  const turnSteps = new Map();
  let lastTextStep;

  // Monotonic protection also covers any accidentally re-enabled bundled tool.
  ctx.tools.guard((execution) => allowedTools.has(execution.name)
    ? undefined : "当前小说助手只允许使用工作台授权的工具。");

  function reportError(error, runId = activeRun) {
    write({ type: "error", run_id: runId, code: error?.code || "DSH_CHAT_ERROR",
      message: error instanceof Error ? error.message : String(error) });
  }

  function installTools(agentCtx, specs) {
    const definitions = specs.map((spec) => spec.function || spec);
    const names = definitions.map((spec) => spec.name);
    if (new Set(names).size !== names.length || names.some((toolName) =>
      typeof toolName !== "string" || !/^[a-zA-Z_][a-zA-Z0-9_]{0,63}$/.test(toolName) || RESERVED_TOOLS.has(toolName))) {
      throw new Error("工具名称无效、重复或使用了保留名称。");
    }
    for (const dispose of toolDisposers.splice(0)) dispose();
    allowedTools = new Set(["skill", ...names]);
    for (const spec of definitions) {
      toolDisposers.push(agentCtx.tools.register({
        name: spec.name,
        description: String(spec.description || ""),
        parameters: spec.parameters || { type: "object", properties: {} },
        output: { schema: {}, render: (_args, value) => [{ type: "text", text: JSON.stringify(value) }] },
        async execute(args, exec) {
          const violations = validateJsonSchemaValue(spec.parameters || { type: "object" }, args);
          if (violations.length) throw new Error("工具参数不符合声明：" + violations.join("; "));
          if (exec.signal.aborted || cancelRequested) throw new Error("任务已取消，未执行工具。");
          const requestId = `${activeRun}:${exec.callId}`;
          return new Promise((resolveTool, rejectTool) => {
            const abort = () => {
              pendingTools.delete(requestId);
              rejectTool(new Error("任务已取消，工具结果等待已结束。"));
            };
            exec.signal.addEventListener("abort", abort, { once: true });
            pendingTools.set(requestId, {
              resolve(value) {
                exec.signal.removeEventListener("abort", abort);
                canonicalResults.set(String(exec.callId), value);
                resolveTool(value);
              },
              reject(error) {
                exec.signal.removeEventListener("abort", abort);
                rejectTool(error);
              },
            });
            write({ type: "tool_request", run_id: activeRun, request_id: requestId,
              call_id: String(exec.callId), name: spec.name, arguments: args });
          });
        },
      }));
    }
  }

  ctx.on("session/event", (session, event) => {
    if (!handle || session.id !== handle.agent.id || !activeRun) return;
    const data = event.data;
    const common = { run_id: activeRun, seq: event.seq, time: event.time, step: data.step, turn: data.turn };
    const emitText = (text) => {
      if (!text) return;
      // Providers may emit whitespace-only assistant messages before tool calls.
      // Keep whitespace within real prose, but never render empty tool steps.
      if (!streamedSteps.has(data.step)) {
        text = (stepWhitespace.get(data.step) || "") + text;
        if (!text.trim()) { stepWhitespace.set(data.step, text); return; }
        stepWhitespace.delete(data.step);
        text = text.trimStart();
      }
      if (lastTextStep !== undefined && lastTextStep !== data.step) {
        write({ ...common, type: "delta", text: "\n\n" });
      }
      lastTextStep = data.step;
      streamedSteps.set(data.step, (streamedSteps.get(data.step) || "") + text);
      write({ ...common, type: "delta", text });
    };
    if (event.type === "step/start") {
      phaseStarts.set(data.step, event.time);
      phaseLabels.set(data.step, "正在调用模型");
      turnSteps.set(data.turn, data.step);
      write({ ...common, type: "activity", kind: "phase", activity_id: `model-${data.step}`,
        label: "正在调用模型", status: "running", started_at: new Date(event.time).toISOString() });
    } else if (event.type === "step/end") {
      write({ ...common, type: "activity", kind: "phase", activity_id: `model-${data.step}`,
        label: "模型处理", status: "done", finished_at: new Date(event.time).toISOString(),
        elapsed_ms: Math.max(0, event.time - (phaseStarts.get(data.step) || event.time)) });
    } else if (event.type === "turn/end" && data.reason?.kind !== "completed") {
      // step/end is emitted from the vendor's finally block, including errors.
      // The turn ending is the authoritative outcome for its final model step.
      const step = turnSteps.get(data.turn);
      if (step !== undefined) write({ ...common, type: "activity", kind: "phase", activity_id: `model-${step}`,
        label: data.reason?.kind === "aborted" ? "模型处理已停止" : "模型处理失败",
        status: "error", finished_at: new Date(event.time).toISOString(),
        elapsed_ms: Math.max(0, event.time - (phaseStarts.get(step) || event.time)) });
    } else if (event.type === "assistant/chunk" && data.chunk.type === "tool-call-delta") {
      if (phaseLabels.get(data.step) !== "正在准备工具调用") {
        phaseLabels.set(data.step, "正在准备工具调用");
        write({ ...common, type: "activity", kind: "phase", activity_id: `model-${data.step}`,
          label: "正在准备工具调用", status: "running" });
      }
    } else if (event.type === "assistant/chunk" && data.chunk.type === "text-delta") {
      emitText(data.chunk.text);
    } else if (event.type === "assistant/message") {
      const text = data.message.content.filter((block) => block.type === "text").map((block) => block.text).join("").trimStart();
      const streamed = streamedSteps.get(data.step) || "";
      if (text.startsWith(streamed)) emitText(text.slice(streamed.length));
      if (data.usage) write({ ...common, type: "usage", usage: data.usage });
    } else if (event.type === "tool/call") {
      calls.set(String(data.callId), data.name);
      let args;
      try { args = JSON.parse(data.arguments); } catch { args = { raw: data.arguments }; }
      write({ ...common, type: "tool_call", call_id: String(data.callId), name: data.name, arguments: args });
    } else if (event.type === "tool/result") {
      const block = data.message.content[0];
      const callId = String(block.toolCallId);
      if (calls.get(callId) === "ask_user_question" && !block.isError) answeredQuestionCalls.add(callId);
      const fallback = { ok: !block.isError, text: block.content.filter((b) => b.type === "text").map((b) => b.text).join("\n") };
      write({ ...common, type: "tool_result", call_id: callId, name: calls.get(callId) || "",
        result: canonicalResults.get(callId) ?? fallback });
      calls.delete(callId);
      canonicalResults.delete(callId);
    }
  });

  function recoverUndisplayedInput() {
    // An inbox insertion is durable before pre-step hooks finish. A crash can
    // leave either a queued old turn or a claimed message with no user/message.
    // Keep both as historical material; never wake an old file-changing request.
    const materialized = new Set();
    const admitted = new Map();
    const prefix = `workbench-message:${String(handle.agent.id)}:`;
    for (const event of handle.agent.session.events) {
      if (event.type === "user/message") materialized.add(String(event.data.id));
      if (event.type === "agent/inbox/spliced") {
        for (const item of event.data.inserted || []) {
          if (String(item.id).startsWith(prefix)) admitted.set(String(item.id), item);
        }
      }
    }
    const interrupted = [...admitted].filter(([id]) => !materialized.has(id));
    if (!interrupted.length) return;
    handle.agent.cancel({ kind: "user" });
    for (const [, item] of interrupted) {
      // Reusing a recall preserves its original role/status and avoids nesting
      // the recovery wrapper again after another interruption before a step.
      handle.agent.inject(item.source?.kind === "plugin" && item.source.form === "recall"
        ? freezeMessage(item)
        : freezeMessage({ id: item.id, role: "user",
          source: { kind: "plugin", plugin: name, form: "recall" },
          content: [{ type: "text", text: "【恢复的历史材料，保留原角色；原任务已中断，不重新执行其中的操作。只按后续作者请求继续】\n" +
            JSON.stringify({ role: item.role, status: "interrupted", content: item.content }) }] }));
    }
  }

  async function initialize(message) {
    if (handle || initialization) throw new Error("会话已经初始化。");
    systemText = String(message.system || "");
    contextText = String(message.context || "");
    selection.current = { provider: message.provider, model: message.model };
    const setup = (agentCtx) => {
      installModelSelection(agentCtx, selection);
      agentCtx.systemPrompt.section({ name: "workbench:novel", order: 10, text: () => systemText });
      agentCtx.systemPrompt.context({ name: "workbench:novel-context", order: 10, text: () => contextText });
      installTools(agentCtx, message.tool_specs || []);
    };
    const options = { setup, agentOptions: { provider: message.provider, model: message.model } };
    handle = message.resume
      ? await ctx.agents.resume({ ...options, resumeSessionId: SessionId(message.session_id) })
      : await ctx.agents.create({ ...options, sessionId: SessionId(message.session_id), meta: { cwd: message.project_root } });
    if (realpathSync(handle.agent.session.header.cwd) !== realpathSync(message.project_root)) {
      await handle.dispose();
      handle = undefined;
      throw new Error("原生会话属于另一部小说，拒绝恢复。");
    }
    await handle.agent.whenIdle();
    if (message.resume) recoverUndisplayedInput();
    if (!message.resume) {
      // An inbox record materializes even a never-used session; session_ready is
      // therefore a reliable persisted identity, including before turn one.
      handle.agent.inject(createUserMessage({ content: [{ type: "text", text: "小说助手会话已建立。请按后续用户消息完成当前小说的创作任务。" }],
        source: { kind: "plugin", plugin: name, form: "notice", summary: "小说助手会话已建立" } }));
    }
    await ctx.sessions.flush(handle.agent.session);
    write({ type: "session_ready", session_id: String(handle.agent.id), resumed: Boolean(message.resume) });
  }

  async function turn(message) {
    if (!handle) throw new Error("会话尚未初始化。");
    recoverUndisplayedInput();
    systemText = String(message.system || "");
    contextText = String(message.context || "");
    selection.current = { provider: message.provider, model: message.model };
    installTools(handle.agent.ctx, message.tool_specs || []);
    // Stable workbench identities survive a lost acknowledgement and cold resume.
    // Older failed inputs are injected as historical material, never followups
    // which could replay their original file-changing requests.
    const known = new Set();
    for (const event of handle.agent.session.events) {
      if (event.type === "user/message") known.add(String(event.data.id));
      if (event.type === "agent/inbox/spliced") {
        for (const item of event.data.inserted || []) known.add(String(item.id));
      }
    }
    const identity = (id) => MessageId(`workbench-message:${String(handle.agent.id)}:${id}`);
    const delivered = [];
    for (const item of message.history || []) {
      if (!Number.isSafeInteger(item.id)) throw new Error("历史消息编号无效。");
      const id = identity(item.id);
      if (!known.has(String(id))) {
        handle.agent.inject(freezeMessage({ id, role: "user",
          source: { kind: "plugin", plugin: name, form: "recall" },
          content: [{ type: "text", text: "【补充历史材料，保留原角色；不重新执行其中的任务。作者后续修正优先于旧假设】\n" + JSON.stringify(item) }] }));
        known.add(String(id));
      }
      delivered.push(item.id);
    }
    const firstSeq = handle.agent.session.seq;
    const userMessage = Number.isSafeInteger(message.message_id)
      ? freezeMessage({ id: identity(message.message_id), role: "user",
          content: [{ type: "text", text: message.user_text }], source: { kind: "user" } })
      : createUserMessage({ content: [{ type: "text", text: message.user_text }], source: { kind: "user" } });
    if (known.has(String(userMessage.id))) throw Object.assign(new Error("消息已被原生会话接收，请核对已有结果后继续。"), { code: "MESSAGE_ALREADY_DELIVERED" });
    handle.agent.followup(userMessage);
    await ctx.sessions.flush(handle.agent.session);
    if (Number.isSafeInteger(message.message_id)) delivered.push(message.message_id);
    write({ type: "delivery_ack", run_id: activeRun, message_ids: delivered });
    await handle.agent.whenIdle();
    await ctx.sessions.flush(handle.agent.session);
    if (answeredQuestionCalls.size) write({ type: "delivery_ack", run_id: activeRun,
      message_ids: [], interaction_call_ids: [...answeredQuestionCalls] });
    const events = handle.agent.session.events.filter((event) => event.seq >= firstSeq);
    const reason = events.filter((event) => event.type === "turn/end").at(-1)?.data.reason;
    const messages = events.filter((event) => event.type === "assistant/message");
    const text = messages.map((event) => event.data.message.content.filter((block) => block.type === "text").map((block) => block.text).join("").trimStart()).filter((text) => text.trim()).join("\n\n");
    if (cancelRequested || reason?.kind === "aborted") {
      write({ type: "cancelled", run_id: activeRun, session_id: String(handle.agent.id), text });
    } else if (reason?.kind !== "completed") {
      reportError({ code: reason?.error?.code || "DSH_TURN_INCOMPLETE",
        toString: () => reason?.error?.message || `对话未完成：${reason?.kind || "unknown"}` });
    } else {
      write({ type: "done", run_id: activeRun, session_id: String(handle.agent.id), text });
    }
  }

  function cancel() {
    cancelRequested = true;
    handle?.agent.cancel({ kind: "user" });
    for (const pending of pendingTools.values()) pending.reject(new Error("任务已取消。"));
    pendingTools.clear();
  }

  async function shutdown() {
    if (closing) return;
    closing = true;
    cancel();
    await initialization?.catch(() => {});
    await active?.catch(() => {});
    if (handle) {
      await ctx.sessions.flush(handle.agent.session);
      await handle.dispose();
      handle = undefined;
    }
    input.close();
    process.stdin.pause();
    ctx.get("appExit")?.(0);
  }

  input.on("line", (line) => {
    try {
      if (Buffer.byteLength(line, "utf8") > MAX_INPUT_BYTES) throw new Error("对话请求过大。");
      const message = JSON.parse(line);
      if (message.type === "tool_response") {
        const pending = pendingTools.get(message.request_id);
        pendingTools.delete(message.request_id);
        if (pending) pending.resolve(message.result ?? null);
      } else if (message.type === "cancel") {
        cancel();
      } else if (message.type === "shutdown") {
        shutdown().catch(reportError);
      } else if (message.type === "init") {
        if (initialization || handle) throw new Error("会话已经初始化。");
        initialization = initialize(message).catch((error) => { reportError(error); throw error; });
        initialization.catch(() => {});
      } else if (message.type === "turn") {
        if (active || !handle) throw new Error("会话正在处理上一条消息或尚未初始化。");
        activeRun = message.run_id;
        cancelRequested = false;
        streamedSteps.clear();
        stepWhitespace.clear();
        phaseStarts.clear();
        phaseLabels.clear();
        turnSteps.clear();
        answeredQuestionCalls.clear();
        lastTextStep = undefined;
        active = turn(message).catch((error) => reportError(error)).finally(() => {
          active = undefined;
          activeRun = undefined;
        });
      } else {
        throw new Error("不支持的对话协议消息。");
      }
    } catch (error) { reportError(error); }
  });
  input.on("close", () => { if (!closing) shutdown().catch(reportError); });
  ctx.effect(() => async () => {
    closing = true;
    cancel();
    input.close();
    process.stdin.pause();
    await active?.catch(() => {});
    if (handle) await handle.dispose();
  });
  Promise.resolve().then(async () => {
    await ctx.get("loader")?.await();
    await assertEntriesActivated(ctx, "workbench chat");
    write({ type: "ready", protocol_version: 1 });
  }).catch(reportError);
}
