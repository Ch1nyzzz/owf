import { runAgentLoop, type AgentContext, type AgentLoopConfig, type AgentMessage, type StreamFn } from "@earendil-works/pi-agent-core";
import type { AssistantMessage, Message, ToolResultMessage } from "@earendil-works/pi-ai";
import { Budget } from "./budget.js";
import { Journal, preview } from "./journal.js";
import { makeSubmitTool, SUBMIT_INSTRUCTION, SUBMIT_TOOL_NAME } from "./structured.js";
import type { AgentOpts, HookState, ModelEntry, NodeOutcome, ToolRegistry, WorkflowHooks } from "./types.js";

const DEFAULT_MAX_TURNS = 30;
const HARD_MAX_TURNS = 200;
const MAX_ONSTOP_CONTINUATIONS = 5;
const MAX_SCHEMA_FAILURES = 3;
const TRANSCRIPT_LIMIT = 32 * 1024;
const RECENT_COMMANDS_LIMIT = 20;

export interface AgentNodeDeps {
	models: Map<string, ModelEntry>;
	tools: ToolRegistry;
	journal: Journal;
	budget: Budget;
	streamFn: StreamFn;
	signal: AbortSignal;
}

function userMsg(text: string): Message {
	return { role: "user", content: text, timestamp: Date.now() } as Message;
}

function assistantText(message: AssistantMessage): string {
	return message.content
		.filter((c): c is Extract<typeof c, { type: "text" }> => c.type === "text")
		.map((c) => c.text)
		.join("");
}

function primaryArg(args: unknown): string {
	if (args && typeof args === "object") {
		const first = Object.values(args as Record<string, unknown>)[0];
		if (typeof first === "string") return first.slice(0, 120);
		return preview(first).slice(0, 120);
	}
	return "";
}

/** Run one agent() node (DSL §3) on the pi agent loop. */
export async function runAgentNode(prompt: string, opts: AgentOpts, seq: number, deps: AgentNodeDeps): Promise<NodeOutcome> {
	const label = opts.label ?? `node-${seq}`;
	const start = Date.now();
	const nodeTokens = { input: 0, output: 0 };
	let lastError: string | undefined;
	const done = (result: string | object | null, status: NodeOutcome["status"], turns: number, messages?: AgentMessage[]): NodeOutcome => {
		if (messages) deps.journal.nodeTranscript(seq, label, messages);
		const outcome: NodeOutcome = { result, status, turns, tokens: { ...nodeTokens }, durationMs: Date.now() - start };
		deps.journal.write({
			type: "node_end",
			node: label,
			seq,
			status,
			turns,
			tokens: outcome.tokens,
			durationMs: outcome.durationMs,
			resultPreview: preview(result),
			...(lastError ? { error: preview(lastError) } : {}),
		});
		return outcome;
	};

	const entry = deps.models.get(opts.model);
	if (!entry) {
		deps.journal.write({ type: "node_start", node: label, seq, model: opts.model, error: "unknown_model" });
		return done(null, "error", 0);
	}
	const tools = [];
	for (const entry of opts.tools ?? []) {
		if (typeof entry === "string") {
			const tool = deps.tools[entry];
			if (!tool) {
				deps.journal.write({ type: "node_start", node: label, seq, model: opts.model, error: `unknown_tool:${entry}` });
				return done(null, "unknown_tool", 0);
			}
			tools.push(tool);
		} else if (entry && typeof entry === "object" && typeof entry.execute === "function" && typeof entry.name === "string") {
			tools.push(entry); // defineTool handle
		} else {
			deps.journal.write({ type: "node_start", node: label, seq, model: opts.model, error: "unknown_tool:invalid_handle" });
			return done(null, "unknown_tool", 0);
		}
	}

	let submitted: object | null = null;
	let schemaFailures = 0;
	if (opts.schema) {
		tools.push(makeSubmitTool(opts.schema, (v) => (submitted = v)));
	}

	const maxTurns = Math.min(opts.maxTurns ?? DEFAULT_MAX_TURNS, HARD_MAX_TURNS);
	const hooks: WorkflowHooks = opts.hooks ?? {};

	deps.journal.write({
		type: "node_start",
		node: label,
		seq,
		model: opts.model,
		tools: (opts.tools ?? []).map((t) => (typeof t === "string" ? t : `defined:${t.name}`)),
		maxTurns,
		has_schema: Boolean(opts.schema),
		hook_names: Object.keys(hooks),
	});

	// --- rollout state visible to hooks (DSL §3.2) ---
	let turn = 0;
	const recentCommands: string[] = [];
	let transcript = "";
	const appendTranscript = (s: string) => {
		transcript += s;
		if (transcript.length > TRANSCRIPT_LIMIT) transcript = transcript.slice(-TRANSCRIPT_LIMIT);
	};
	const state = (): HookState => ({
		turn,
		elapsedMs: Date.now() - start,
		tokensSpent: nodeTokens.input + nodeTokens.output,
		recentCommands: [...recentCommands],
		transcript,
	});

	const fireHook = async <T>(name: string, fn: (() => Promise<T> | T) | undefined): Promise<T | undefined> => {
		if (!fn) return undefined;
		try {
			return await fn();
		} catch (err) {
			deps.journal.write({ type: "hook_error", node: label, turn, hook: name, error: String(err) });
			return undefined;
		}
	};

	const steering: string[] = [];
	let stopRequested: NodeOutcome["status"] | null = null;
	let continuations = 0;
	const toolStartTimes = new Map<string, number>();
	const toolArgs = new Map<string, unknown>();

	const context: AgentContext = {
		systemPrompt: opts.schema ? opts.system + SUBMIT_INSTRUCTION : opts.system,
		messages: [],
		tools,
	};

	const config: AgentLoopConfig = {
		model: entry.model,
		apiKey: entry.apiKey,
		getApiKey: entry.resolveKey ? () => entry.resolveKey!() : undefined,
		// the Codex responses API rejects temperature outright
		temperature: entry.model.api === "openai-codex-responses" ? undefined : (opts.temperature ?? 0.0),
		// thinkingLevel is dropped for non-reasoning models (openai-compat endpoints may reject the param)
		reasoning: opts.thinkingLevel && opts.thinkingLevel !== "off" && entry.model.reasoning ? opts.thinkingLevel : undefined,
		convertToLlm: (messages) => messages as Message[],

		beforeToolCall: async ({ toolCall, args }) => {
			recentCommands.push(`${toolCall.name}:${primaryArg(args)}`);
			if (recentCommands.length > RECENT_COMMANDS_LIMIT) recentCommands.shift();
			const verdict = await fireHook("preToolUse", hooks.preToolUse && (() => hooks.preToolUse!({ toolName: toolCall.name, args }, state())));
			if (verdict && typeof verdict === "object" && verdict.block) {
				deps.journal.write({ type: "hook_fire", node: label, turn, hook: "preToolUse", action: "block", payloadPreview: preview(verdict.block) });
				return { block: true, reason: verdict.block };
			}
			return undefined;
		},

		afterToolCall: async ({ toolCall, result, isError }) => {
			const contentText = result.content
				.filter((c): c is Extract<typeof c, { type: "text" }> => c.type === "text")
				.map((c) => c.text)
				.join("\n");
			if (toolCall.name === SUBMIT_TOOL_NAME && isError) {
				schemaFailures += 1;
				if (schemaFailures >= MAX_SCHEMA_FAILURES) stopRequested = "schema_failed";
			}
			const verdict = await fireHook(
				"postToolUse",
				hooks.postToolUse && (() => hooks.postToolUse!({ toolName: toolCall.name, args: toolCall.arguments }, { content: contentText, isError, details: result.details }, state())),
			);
			if (verdict && typeof verdict === "object" && verdict.inject) {
				steering.push(verdict.inject);
				deps.journal.write({ type: "hook_fire", node: label, turn, hook: "postToolUse", action: "inject", payloadPreview: preview(verdict.inject) });
			}
			return undefined;
		},

		shouldStopAfterTurn: async ({ message }) => {
			turn += 1;
			if (deps.budget.exceeded()) {
				stopRequested = "budget_exceeded";
				return true;
			}
			if (stopRequested) return true;
			if (turn >= maxTurns) {
				stopRequested = "maxTurns";
				return true;
			}
			const verdict = await fireHook("onTurn", hooks.onTurn && (() => hooks.onTurn!(state())));
			if (verdict && typeof verdict === "object") {
				if (verdict.inject) {
					steering.push(verdict.inject);
					deps.journal.write({ type: "hook_fire", node: label, turn, hook: "onTurn", action: "inject", payloadPreview: preview(verdict.inject) });
				}
				if (verdict.stop) {
					deps.journal.write({ type: "hook_fire", node: label, turn, hook: "onTurn", action: "stop" });
					return true;
				}
			}
			return false;
		},

		getSteeringMessages: async () => {
			const msgs = steering.splice(0).map(userMsg);
			return msgs as AgentMessage[];
		},

		getFollowUpMessages: async () => {
			if (submitted !== null || stopRequested) return [];
			if (!hooks.onStop || continuations >= MAX_ONSTOP_CONTINUATIONS) return [];
			const verdict = await fireHook("onStop", () => hooks.onStop!(state()));
			if (verdict && typeof verdict === "object" && verdict.continue) {
				continuations += 1;
				deps.journal.write({ type: "hook_fire", node: label, turn, hook: "onStop", action: "continue", payloadPreview: preview(verdict.continue) });
				return [userMsg(verdict.continue)] as AgentMessage[];
			}
			return [];
		},
	};

	const emit = async (event: import("@earendil-works/pi-agent-core").AgentEvent): Promise<void> => {
		switch (event.type) {
			case "message_end": {
				const m = event.message as AssistantMessage;
				if ((m as { role?: string }).role === "assistant" && m.errorMessage) lastError = m.errorMessage;
				if ((m as { role?: string }).role === "assistant" && m.usage) {
					nodeTokens.input += m.usage.input ?? 0;
					nodeTokens.output += m.usage.output ?? 0;
					deps.budget.addUsage(m.usage);
					deps.journal.write({
						type: "turn",
						node: label,
						turn: turn + 1,
						tokens: { input: m.usage.input ?? 0, output: m.usage.output ?? 0 },
						toolCalls: m.content.filter((c) => c.type === "toolCall").map((c) => ({ tool: (c as { name: string }).name, argsPreview: preview((c as { arguments: unknown }).arguments).slice(0, 200) })),
					});
					const text = assistantText(m);
					if (text) appendTranscript(`\n[assistant] ${text}\n`);
				}
				break;
			}
			case "tool_execution_start":
				toolStartTimes.set(event.toolCallId, Date.now());
				toolArgs.set(event.toolCallId, event.args);
				break;
			case "tool_execution_end": {
				const t0 = toolStartTimes.get(event.toolCallId) ?? Date.now();
				const resultPreviewText = preview(event.result?.content ?? event.result);
				deps.journal.write({
					type: "tool_call",
					node: label,
					turn: turn + 1,
					tool: event.toolName,
					argsPreview: preview(toolArgs.get(event.toolCallId)).slice(0, 400),
					resultPreview: resultPreviewText.slice(0, 400),
					isError: event.isError,
					durationMs: Date.now() - t0,
				});
				appendTranscript(`\n[tool ${event.toolName}] ${resultPreviewText.slice(0, 1000)}\n`);
				break;
			}
		}
	};

	let messages: AgentMessage[];
	try {
		messages = await runAgentLoop([userMsg(prompt) as AgentMessage], context, config, emit, deps.signal, deps.streamFn);
	} catch (err) {
		deps.journal.write({ type: "hook_error", node: label, turn, hook: "loop", error: String(err) });
		return done(null, "error", turn);
	}

	// runAgentLoop copies the context internally; the rollout transcript is its RETURN VALUE.
	const lastAssistant = [...messages].reverse().find((m) => (m as { role?: string }).role === "assistant") as AssistantMessage | undefined;

	if (deps.signal.aborted) return done(null, "aborted", turn, messages);
	if (opts.schema) {
		if (submitted !== null) return done(submitted, "ok", turn, messages);
		const status = stopRequested === "budget_exceeded" ? "budget_exceeded" : stopRequested === "schema_failed" ? "schema_failed" : stopRequested === "maxTurns" ? "maxTurns" : "error";
		return done(null, status, turn, messages);
	}

	if (lastAssistant?.stopReason === "error") return done(null, "error", turn, messages);
	const text = lastAssistant ? assistantText(lastAssistant) : "";
	// Text nodes: any produced text counts as the answer even if we stopped early
	// (maxTurns / budget); the status still records how the node ended.
	const status: NodeOutcome["status"] = stopRequested ?? "ok";
	return done(text || null, text ? status : status === "ok" ? "error" : status, turn, messages);
}
