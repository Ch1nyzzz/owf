import type { StreamFn } from "@earendil-works/pi-agent-core";
import { runAgentNode } from "./agent-node.js";
import type { Budget } from "./budget.js";
import { Journal, preview } from "./journal.js";
import type { AgentOpts, ModelEntry, TaskPayload, ToolRegistry, WorkflowCtx } from "./types.js";

const AGENT_CONCURRENCY = 8;

class Semaphore {
	private queue: Array<() => void> = [];
	private active = 0;
	constructor(private readonly limit: number) {}
	async acquire(): Promise<void> {
		if (this.active < this.limit) {
			this.active += 1;
			return;
		}
		await new Promise<void>((resolve) => this.queue.push(resolve));
		this.active += 1;
	}
	release(): void {
		this.active -= 1;
		this.queue.shift()?.();
	}
}

export interface CtxDeps {
	task: TaskPayload;
	models: Map<string, ModelEntry>;
	tools: ToolRegistry;
	journal: Journal;
	budget: Budget;
	streamFn: StreamFn;
	signal: AbortSignal;
}

export class BudgetExceededError extends Error {
	constructor() {
		super("budget exceeded: no further agent() calls allowed");
	}
}

export function buildCtx(deps: CtxDeps): WorkflowCtx {
	const sem = new Semaphore(AGENT_CONCURRENCY);
	let seq = 0;
	let hookDepth = 0;

	const agent = async (prompt: string, opts: AgentOpts): Promise<string | object | null> => {
		if (deps.budget.exceeded()) throw new BudgetExceededError();
		if (typeof prompt !== "string" || !opts || typeof opts.system !== "string" || typeof opts.model !== "string") {
			throw new Error("agent(prompt, opts): prompt must be a string; opts.system and opts.model are required strings");
		}
		// depth-1 rule (DSL §3.1): nodes spawned from inside hooks may not declare hooks.
		const effectiveOpts = hookDepth > 0 && opts.hooks ? { ...opts, hooks: undefined } : opts;
		const mySeq = ++seq;
		await sem.acquire();
		try {
			const wrappedHooks = effectiveOpts.hooks
				? Object.fromEntries(
						Object.entries(effectiveOpts.hooks).map(([name, fn]) => [
							name,
							async (...args: unknown[]) => {
								hookDepth += 1;
								try {
									return await (fn as (...a: unknown[]) => unknown)(...args);
								} finally {
									hookDepth -= 1;
								}
							},
						]),
					)
				: undefined;
			const outcome = await runAgentNode(prompt, { ...effectiveOpts, hooks: wrappedHooks }, mySeq, deps);
			return outcome.result;
		} finally {
			sem.release();
		}
	};

	const pipeline = async (items: unknown[], ...stages: Array<(prev: unknown, item: unknown, index: number) => unknown>): Promise<unknown[]> => {
		if (!Array.isArray(items)) throw new Error("pipeline(items, ...stages): items must be an array");
		return Promise.all(
			items.map(async (item, index) => {
				let prev: unknown = item;
				for (const stage of stages) {
					try {
						prev = await stage(prev, item, index);
					} catch (err) {
						deps.journal.write({ type: "log", message: `pipeline item ${index} dropped: ${preview(String(err))}` });
						return null;
					}
					if (prev === null || prev === undefined) return null;
				}
				return prev;
			}),
		);
	};

	const parallel = async (thunks: Array<() => Promise<unknown>>): Promise<unknown[]> => {
		if (!Array.isArray(thunks)) throw new Error("parallel(thunks): thunks must be an array of functions");
		return Promise.all(
			thunks.map(async (thunk, index) => {
				try {
					return await thunk();
				} catch (err) {
					deps.journal.write({ type: "log", message: `parallel thunk ${index} failed: ${preview(String(err))}` });
					return null;
				}
			}),
		);
	};

	return {
		task: Object.freeze(structuredClone(deps.task)),
		agent,
		pipeline,
		parallel,
		log: (message: string) => deps.journal.write({ type: "log", message: preview(String(message)) }),
		budget: {
			totalTokens: deps.budget.maxTokens,
			spentTokens: () => deps.budget.spent(),
			remainingTokens: () => deps.budget.remainingTokens(),
			elapsedMs: () => deps.budget.elapsedMs(),
		},
	};
}
