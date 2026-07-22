import assert from "node:assert/strict";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import type { StreamFn } from "@earendil-works/pi-agent-core";
import { createAssistantMessageEventStream, Type, type AssistantMessage, type Context } from "@earendil-works/pi-ai";
import { Budget } from "../src/budget.js";
import { buildCtx, type CtxDeps } from "../src/dsl.js";
import { Journal } from "../src/journal.js";
import type { ModelEntry, ToolRegistry } from "../src/types.js";

function mkAssistant(content: AssistantMessage["content"], stopReason: AssistantMessage["stopReason"] = "stop"): AssistantMessage {
	return {
		role: "assistant", content, api: "openai-completions", provider: "mock", model: "m",
		usage: { input: 5, output: 5, cacheRead: 0, cacheWrite: 0, totalTokens: 10, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
		stopReason, timestamp: 0,
	};
}
const text = (t: string) => ({ type: "text", text: t }) as const;
const call = (id: string, name: string, args: Record<string, unknown>) => ({ type: "toolCall", id, name, arguments: args }) as const;

function mockStream(script: AssistantMessage[]): StreamFn {
	return ((_m: unknown, _c: Context) => {
		const s = createAssistantMessageEventStream();
		s.push({ type: "done", message: script.shift() ?? mkAssistant([text("(exhausted)")]) } as never);
		return s;
	}) as StreamFn;
}

function mkDeps(streamFn: StreamFn): { deps: CtxDeps; outDir: string } {
	const outDir = mkdtempSync(join(tmpdir(), "owf-dt-"));
	const echoLog: string[] = [];
	const registry: ToolRegistry = {
		echo: {
			name: "echo", label: "Echo", description: "d",
			parameters: Type.Object({ text: Type.String() }),
			execute: async (_id: string, p: { text: string }) => {
				echoLog.push(p.text);
				return { content: [{ type: "text" as const, text: `echo: ${p.text}` }], details: {} };
			},
		} as never,
	};
	const deps: CtxDeps = {
		task: { id: "t", instruction: "x" },
		models: new Map<string, ModelEntry>([["mock", { key: "mock", model: { id: "m", name: "mock", api: "openai-completions", provider: "mock", baseUrl: "http://x", reasoning: false, input: ["text"], cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 100000, maxTokens: 4096 } as ModelEntry["model"], apiKey: "k" }]]),
		tools: registry, journal: new Journal(outDir), budget: new Budget(1_000_000, 60_000), streamFn,
		signal: new AbortController().signal,
	};
	return { deps, outDir };
}

test("runTool: direct harness-primitive invocation, journaled", async () => {
	const { deps, outDir } = mkDeps(mockStream([]));
	const ctx = buildCtx(deps);
	const out = await ctx.runTool("echo", { text: "direct" });
	assert.equal(out, "echo: direct");
	const events = readFileSync(join(outDir, "journal.jsonl"), "utf8");
	assert.ok(events.includes('"node":"runTool"'));
	await assert.rejects(() => ctx.runTool("nope", {}), /unknown harness tool/);
});

test("defineTool: composition handler used by an agent node", async () => {
	const { deps } = mkDeps(mockStream([
		mkAssistant([call("c1", "double_echo", { text: "hi" })], "toolUse"),
		mkAssistant([text("done")]),
	]));
	const ctx = buildCtx(deps);
	const doubleEcho = ctx.defineTool({
		name: "double_echo",
		description: "echo twice via the harness echo primitive",
		schema: { type: "object", properties: { text: { type: "string" } }, required: ["text"] },
		handler: async ({ text: t }) => {
			const a = await ctx.runTool("echo", { text: String(t) });
			const b = await ctx.runTool("echo", { text: String(t).toUpperCase() });
			return `${a} | ${b}`;
		},
	});
	const out = await ctx.agent("go", { system: "s", model: "mock", tools: [doubleEcho] });
	assert.equal(out, "done");
});

test("defineTool: agent-as-a-tool handler", async () => {
	const { deps } = mkDeps(mockStream([
		mkAssistant([call("c1", "ask_expert", { q: "2+2?" })], "toolUse"), // outer node calls the tool
		mkAssistant([text("the expert says 4")]),                          // inner agent (inside handler)
		mkAssistant([text("final: 4")]),                                    // outer node continues
	]));
	const ctx = buildCtx(deps);
	const askExpert = ctx.defineTool({
		name: "ask_expert",
		description: "consult a sub-agent",
		schema: { type: "object", properties: { q: { type: "string" } }, required: ["q"] },
		handler: async ({ q }) => ctx.agent(String(q), { system: "expert", model: "mock" }),
	});
	const out = await ctx.agent("solve", { system: "s", model: "mock", tools: [askExpert] });
	assert.equal(out, "final: 4");
});

test("invalid tool handle fails the node as unknown_tool", async () => {
	const { deps, outDir } = mkDeps(mockStream([]));
	const ctx = buildCtx(deps);
	const out = await ctx.agent("go", { system: "s", model: "mock", tools: [{ bogus: true } as never] });
	assert.equal(out, null);
	const events = readFileSync(join(outDir, "journal.jsonl"), "utf8");
	assert.ok(events.includes("unknown_tool"));
});
