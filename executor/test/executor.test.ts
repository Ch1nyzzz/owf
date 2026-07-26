import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import type { StreamFn } from "@earendil-works/pi-agent-core";
import { createAssistantMessageEventStream, Type, type AssistantMessage, type Context } from "@earendil-works/pi-ai";
import { Budget } from "../src/budget.js";
import { BudgetExceededError, buildCtx, type CtxDeps } from "../src/dsl.js";
import { Journal } from "../src/journal.js";
import { loadWorkflow } from "../src/sandbox.js";
import type { ModelEntry, ToolRegistry } from "../src/types.js";

// ---------- mock plumbing ----------

function mkAssistant(content: AssistantMessage["content"], stopReason: AssistantMessage["stopReason"] = "stop", usage?: Partial<AssistantMessage["usage"]>): AssistantMessage {
	return {
		role: "assistant",
		content,
		api: "openai-completions",
		provider: "mock",
		model: "mock-model",
		usage: {
			input: usage?.input ?? 10,
			output: usage?.output ?? 5,
			cacheRead: 0,
			cacheWrite: 0,
			totalTokens: (usage?.input ?? 10) + (usage?.output ?? 5),
			cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
		},
		stopReason,
		timestamp: 0,
	};
}

const text = (t: string) => ({ type: "text", text: t }) as const;
const call = (id: string, name: string, args: Record<string, unknown>) => ({ type: "toolCall", id, name, arguments: args }) as const;

/** Scripted streamFn: pops one response per LLM call; records each request Context. */
function mockStream(script: Array<AssistantMessage | ((c: Context) => AssistantMessage)>): { streamFn: StreamFn; contexts: Context[] } {
	const contexts: Context[] = [];
	const streamFn: StreamFn = ((_model: unknown, context: Context) => {
		// tools carry functions — clone only the assertable parts
		contexts.push({ systemPrompt: context.systemPrompt, messages: structuredClone(context.messages), tools: [] } as Context);
		const next = script.shift();
		const message = typeof next === "function" ? next(context) : (next ?? mkAssistant([text("(script exhausted)")]));
		const stream = createAssistantMessageEventStream();
		stream.push({ type: "done", message } as never);
		return stream;
	}) as StreamFn;
	return { streamFn, contexts };
}

function mkDeps(streamFn: StreamFn, opts?: { tools?: ToolRegistry; maxTokens?: number }): { deps: CtxDeps; outDir: string } {
	const outDir = mkdtempSync(join(tmpdir(), "owf-test-"));
	const models = new Map<string, ModelEntry>([
		["mock", { key: "mock", model: { id: "mock-model", name: "mock", api: "openai-completions", provider: "mock", baseUrl: "http://mock", reasoning: false, input: ["text"], cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 100000, maxTokens: 4096 } as ModelEntry["model"], apiKey: "k", sut: true }],
	]);
	const deps: CtxDeps = {
		task: { id: "t1", instruction: "do it" },
		models,
		tools: opts?.tools ?? {},
		journal: new Journal(outDir),
		budget: new Budget(opts?.maxTokens ?? 1_000_000, 60_000),
		streamFn,
		signal: new AbortController().signal,
	};
	return { deps, outDir };
}

function journalEvents(outDir: string): Array<Record<string, unknown>> {
	return readFileSync(join(outDir, "journal.jsonl"), "utf8")
		.trim()
		.split("\n")
		.filter(Boolean)
		.map((l) => JSON.parse(l));
}

const echoTool = (log: string[]) => ({
	echo: {
		name: "echo",
		label: "Echo",
		description: "echo text",
		parameters: Type.Object({ text: Type.String() }),
		execute: async (_id: string, params: { text: string }) => {
			log.push(params.text);
			return { content: [{ type: "text" as const, text: `echo: ${params.text}` }], details: {} };
		},
	},
});

// ---------- tests ----------

test("simple text node returns final text and accounts tokens", async () => {
	const { streamFn } = mockStream([mkAssistant([text("hello world")])]);
	const { deps, outDir } = mkDeps(streamFn);
	const ctx = buildCtx(deps);
	const out = await ctx.agent("hi", { system: "sys", model: "mock" });
	assert.equal(out, "hello world");
	const events = journalEvents(outDir);
	const nodeEnd = events.find((e) => e.type === "node_end") as { status: string; tokens: { input: number; output: number } };
	assert.equal(nodeEnd.status, "ok");
	assert.equal(nodeEnd.tokens.input, 10);
	assert.equal(deps.budget.spent(), 15);
});

test("preToolUse block prevents execution; model sees block reason", async () => {
	const executed: string[] = [];
	const { streamFn, contexts } = mockStream([
		mkAssistant([call("c1", "echo", { text: "secret" })], "toolUse"),
		mkAssistant([text("done")]),
	]);
	const { deps, outDir } = mkDeps(streamFn, { tools: echoTool(executed) as unknown as ToolRegistry });
	const ctx = buildCtx(deps);
	const out = await ctx.agent("go", {
		system: "sys",
		model: "mock",
		tools: ["echo"],
		hooks: { preToolUse: (c) =>

((c.args as { text: string }).text === "secret" ? { block: "not allowed" } : undefined) },
	});
	assert.equal(out, "done");
	assert.deepEqual(executed, []); // tool never ran
	const secondCall = contexts[1];
	const toolResult = secondCall.messages.find((m) => (m as { role: string }).role === "toolResult") as { content: Array<{ text: string }>; isError: boolean };
	assert.ok(toolResult.content[0].text.includes("not allowed"));
	const events = journalEvents(outDir);
	assert.ok(events.some((e) => e.type === "hook_fire" && e.hook === "preToolUse" && e.action === "block"));
});

test("postToolUse inject adds a user message before the next LLM call", async () => {
	const executed: string[] = [];
	const { streamFn, contexts } = mockStream([
		mkAssistant([call("c1", "echo", { text: "ok" })], "toolUse"),
		mkAssistant([text("finished")]),
	]);
	const { deps } = mkDeps(streamFn, { tools: echoTool(executed) as unknown as ToolRegistry });
	const ctx = buildCtx(deps);
	const out = await ctx.agent("go", {
		system: "sys",
		model: "mock",
		tools: ["echo"],
		hooks: { postToolUse: (_c, r) => (r.isError ? undefined : { inject: "note: verify next" }) },
	});
	assert.equal(out, "finished");
	assert.deepEqual(executed, ["ok"]);
	const secondCall = contexts[1];
	const userMsgs = secondCall.messages.filter((m) => (m as { role: string }).role === "user").map((m) => (m as { content: string }).content);
	assert.ok(userMsgs.includes("note: verify next"));
});

// The read cap that keeps optimizer.js's reader subagents inside their context window
// (workflows/_meta/optimizer.js, READ_CAP_BYTES) rests on the two hooks composing: only
// postToolUse sees how much a tool handed back, and only preToolUse can refuse the next
// call. Nothing else in the stack catches a context overflow, so pin the contract here.
test("postToolUse counting + preToolUse blocking enforces a cumulative read cap", async () => {
	const executed: string[] = [];
	const toolCall = (n: number) => mkAssistant([call(`c${n}`, "echo", { text: "0123456789" })], "toolUse");
	const { streamFn, contexts } = mockStream([toolCall(1), toolCall(2), toolCall(3), mkAssistant([text("done")])]);
	const { deps, outDir } = mkDeps(streamFn, { tools: echoTool(executed) as unknown as ToolRegistry });
	const ctx = buildCtx(deps);

	const CAP = 20; // each echo returns "echo: 0123456789" = 16 bytes, so the cap falls mid-stream
	let bytesRead = 0;
	let warned = false;
	const out = await ctx.agent("go", {
		system: "sys",
		model: "mock",
		tools: ["echo"],
		hooks: {
			postToolUse: (_c, r) => {
				bytesRead += r.content.length;
				if (!warned && bytesRead > CAP * 0.75) {
					warned = true;
					return { inject: "converge now" };
				}
			},
			preToolUse: () => (bytesRead > CAP ? { block: "read cap reached" } : undefined),
		},
	});

	assert.equal(out, "done");
	// 16 bytes after the first call clears 0.75*CAP but not CAP; 32 after the second does.
	assert.deepEqual(executed, ["0123456789", "0123456789"]);
	assert.ok(contexts[1].messages.some((m) => (m as { content: unknown }).content === "converge now"));
	const blocked = contexts[3].messages.filter((m) => (m as { role: string }).role === "toolResult").at(-1) as { content: Array<{ text: string }> };
	assert.ok(blocked.content[0].text.includes("read cap reached"));
	// The node survives the cap — that is the point: a truncated report still lands.
	const nodeEnd = journalEvents(outDir).find((e) => e.type === "node_end") as { status: string };
	assert.equal(nodeEnd.status, "ok");
});

test("onStop continue pushes the rollout onward, capped", async () => {
	let stops = 0;
	const { streamFn, contexts } = mockStream([
		mkAssistant([text("draft answer")]),
		mkAssistant([text("final answer")]),
	]);
	const { deps } = mkDeps(streamFn);
	const ctx = buildCtx(deps);
	const out = await ctx.agent("solve", {
		system: "sys",
		model: "mock",
		hooks: {
			onStop: () => {
				stops += 1;
				return stops === 1 ? { continue: "are you sure? double-check" } : undefined;
			},
		},
	});
	assert.equal(out, "final answer");
	assert.equal(stops, 2); // fired again after second answer, returned undefined
	assert.equal(contexts.length, 2);
	const secondCall = contexts[1];
	const userMsgs = secondCall.messages.filter((m) => (m as { role: string }).role === "user").map((m) => (m as { content: string }).content);
	assert.ok(userMsgs.includes("are you sure? double-check"));
});

test("schema node returns validated object via submit_result", async () => {
	const { streamFn } = mockStream([
		mkAssistant([call("c1", "submit_result", { answer: 42, unit: "kg" })], "toolUse"),
	]);
	const { deps } = mkDeps(streamFn);
	const ctx = buildCtx(deps);
	const out = await ctx.agent("solve", {
		system: "sys",
		model: "mock",
		schema: { type: "object", properties: { answer: { type: "number" }, unit: { type: "string" } }, required: ["answer"] },
	});
	assert.deepEqual(out, { answer: 42, unit: "kg" });
});

test("schema validation failure feeds error back, then succeeds", async () => {
	const { streamFn, contexts } = mockStream([
		mkAssistant([call("c1", "submit_result", { answer: "not-a-number" })], "toolUse"),
		mkAssistant([call("c2", "submit_result", { answer: 7 })], "toolUse"),
	]);
	const { deps } = mkDeps(streamFn);
	const ctx = buildCtx(deps);
	const out = await ctx.agent("solve", {
		system: "sys",
		model: "mock",
		schema: { type: "object", properties: { answer: { type: "number" } }, required: ["answer"] },
	});
	assert.deepEqual(out, { answer: 7 });
	const secondCall = contexts[1];
	const toolResult = secondCall.messages.find((m) => (m as { role: string }).role === "toolResult") as { isError?: boolean };
	assert.equal(toolResult.isError, true);
});

test("maxTurns stops a tool-looping node; text so far is returned with maxTurns status", async () => {
	const executed: string[] = [];
	const looping = () => mkAssistant([text("thinking..."), call(`c${Math.floor(executed.length)}`, "echo", { text: "again" })], "toolUse");
	const { streamFn } = mockStream([looping(), looping(), looping(), looping(), looping()]);
	const { deps, outDir } = mkDeps(streamFn, { tools: echoTool(executed) as unknown as ToolRegistry });
	const ctx = buildCtx(deps);
	const out = await ctx.agent("go", { system: "sys", model: "mock", tools: ["echo"], maxTurns: 3 });
	assert.equal(out, "thinking...");
	const events = journalEvents(outDir);
	const nodeEnd = events.find((e) => e.type === "node_end") as { status: string; turns: number };
	assert.equal(nodeEnd.status, "maxTurns");
	assert.equal(nodeEnd.turns, 3);
});

test("token budget exhaustion fails the node and blocks further agent() calls", async () => {
	const big = mkAssistant([text("chunk"), call("c1", "echo", { text: "x" })], "toolUse", { input: 600, output: 600 });
	const { streamFn } = mockStream([big, big, big]);
	const executed: string[] = [];
	const { deps, outDir } = mkDeps(streamFn, { tools: echoTool(executed) as unknown as ToolRegistry, maxTokens: 1000 });
	const ctx = buildCtx(deps);
	await ctx.agent("go", { system: "sys", model: "mock", tools: ["echo"] });
	const events = journalEvents(outDir);
	const nodeEnd = events.find((e) => e.type === "node_end") as { status: string };
	assert.equal(nodeEnd.status, "budget_exceeded");
	await assert.rejects(() => ctx.agent("again", { system: "sys", model: "mock" }), BudgetExceededError);
});

test("unknown tool name fails the node cleanly", async () => {
	const { streamFn } = mockStream([]);
	const { deps, outDir } = mkDeps(streamFn);
	const ctx = buildCtx(deps);
	const out = await ctx.agent("go", { system: "sys", model: "mock", tools: ["nope"] });
	assert.equal(out, null);
	const nodeEnd = journalEvents(outDir).find((e) => e.type === "node_end") as { status: string };
	assert.equal(nodeEnd.status, "unknown_tool");
});

test("pipeline drops nulls per-item; parallel maps errors to null", async () => {
	const { streamFn } = mockStream([]);
	const { deps } = mkDeps(streamFn);
	const ctx = buildCtx(deps);
	const piped = await ctx.pipeline(
		[1, 2, 3],
		(x) => ((x as number) === 2 ? null : (x as number) * 10),
		(x) => (x as number) + 1,
	);
	assert.deepEqual(piped, [11, null, 31]);
	const par = await ctx.parallel([async () => "a", async () => { throw new Error("boom"); }, async () => "c"]);
	assert.deepEqual(par, ["a", null, "c"]);
});

test("sandbox: loads valid workflow, bans Date.now and Math.random", async () => {
	const dir = mkdtempSync(join(tmpdir(), "owf-wf-"));
	const good = join(dir, "good.js");
	writeFileSync(
		good,
		`export const meta = { name: 'test-wf', version: 1 }
export default async function run(ctx) {
  const items = [1, 2].map((x) => x * 2)
  return { sum: items[0] + items[1], task: ctx.task.id }
}
`,
	);
	const wf = loadWorkflow(good);
	assert.equal(wf.meta.name, "test-wf");
	const result = await wf.run({ task: { id: "t9" } });
	// vm-realm objects have a different Object prototype; compare by JSON (run.ts serializes anyway)
	assert.equal(JSON.stringify(result), JSON.stringify({ sum: 6, task: "t9" }));

	const bad = join(dir, "bad.js");
	writeFileSync(
		bad,
		`export const meta = { name: 'bad-wf' }
export default async function run() { return Date.now() }
`,
	);
	const wfBad = loadWorkflow(bad);
	await assert.rejects(() => wfBad.run({}), /Date\.now is banned/);

	const bad2 = join(dir, "bad2.js");
	writeFileSync(
		bad2,
		`export const meta = { name: 'bad2-wf' }
export default async function run() { return Math.random() }
`,
	);
	const wfBad2 = loadWorkflow(bad2);
	await assert.rejects(() => wfBad2.run({}), /Math\.random is banned/);
});

test("hook that throws is ignored and journaled; rollout continues", async () => {
	const { streamFn } = mockStream([mkAssistant([text("survived")])]);
	const { deps, outDir } = mkDeps(streamFn);
	const ctx = buildCtx(deps);
	const out = await ctx.agent("go", {
		system: "sys",
		model: "mock",
		hooks: { onTurn: () => { throw new Error("hook bug"); } },
	});
	assert.equal(out, "survived");
	assert.ok(journalEvents(outDir).some((e) => e.type === "hook_error" && e.hook === "onTurn"));
});
