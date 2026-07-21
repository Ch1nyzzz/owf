import type { AgentTool } from "@earendil-works/pi-agent-core";
import type { Model } from "@earendil-works/pi-ai";

/** A model entry resolved from configs/models.yaml. */
export interface ModelEntry {
	key: string;
	model: Model<any>;
	apiKey: string | undefined;
}

/** Read-only rollout view passed to hooks (DSL §3.2). */
export interface HookState {
	turn: number;
	elapsedMs: number;
	tokensSpent: number;
	recentCommands: string[];
	transcript: string;
}

export interface HookToolCall {
	toolName: string;
	args: unknown;
}

export interface HookToolResult {
	content: string;
	isError: boolean;
	details?: unknown;
}

export interface WorkflowHooks {
	preToolUse?: (call: HookToolCall, state: HookState) => Promise<{ block?: string } | undefined | void> | { block?: string } | undefined | void;
	postToolUse?: (
		call: HookToolCall,
		result: HookToolResult,
		state: HookState,
	) => Promise<{ inject?: string } | undefined | void> | { inject?: string } | undefined | void;
	onTurn?: (state: HookState) => Promise<{ stop?: boolean; inject?: string } | undefined | void> | { stop?: boolean; inject?: string } | undefined | void;
	onStop?: (state: HookState) => Promise<{ continue?: string } | undefined | void> | { continue?: string } | undefined | void;
}

/** agent() options (DSL §3). */
export interface AgentOpts {
	system: string;
	model: string;
	tools?: string[];
	maxTurns?: number;
	temperature?: number;
	thinkingLevel?: "off" | "minimal" | "low" | "medium" | "high";
	schema?: object;
	label?: string;
	hooks?: WorkflowHooks;
}

export type NodeStatus = "ok" | "error" | "maxTurns" | "schema_failed" | "budget_exceeded" | "aborted" | "unknown_tool";

export interface NodeOutcome {
	result: string | object | null;
	status: NodeStatus;
	turns: number;
	tokens: { input: number; output: number };
	durationMs: number;
}

/** Task payload given to the workflow (frozen). */
export interface TaskPayload {
	id: string;
	instruction: string;
	[key: string]: unknown;
}

export interface WorkflowCtx {
	task: TaskPayload;
	agent: (prompt: string, opts: AgentOpts) => Promise<string | object | null>;
	pipeline: (items: unknown[], ...stages: Array<(prev: unknown, item: unknown, index: number) => unknown>) => Promise<unknown[]>;
	parallel: (thunks: Array<() => Promise<unknown>>) => Promise<unknown[]>;
	log: (message: string) => void;
	budget: {
		totalTokens: number;
		spentTokens: () => number;
		remainingTokens: () => number;
		elapsedMs: () => number;
	};
}

export type ToolRegistry = Record<string, AgentTool<any>>;
