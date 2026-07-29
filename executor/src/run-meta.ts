import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { streamSimple } from "@earendil-works/pi-ai/compat";
import { withTransportRetry } from "./retry.js";
import type { StreamFn } from "@earendil-works/pi-agent-core";
import * as piAgentCore from "@earendil-works/pi-agent-core";
import { runAgentNode, type AgentNodeDeps } from "./agent-node.js";
import { Budget } from "./budget.js";
import { Journal, preview } from "./journal.js";
import { loadModels } from "./models.js";
import { buildRegistry } from "./tools/registry.js";
import type { TaskPayload } from "./types.js";

/**
 * Meta-harness arm entry: run one FREE-FORM agent module against one task.
 *
 * The workflow arm runs candidates through the sandboxed DSL (run.ts); this
 * entry runs candidates that are plain JS modules — the evolvable substrate of
 * the meta-harness baseline. Everything else is the SAME frozen stack: models
 * registry (SUT-filtered), tool registry, Journal, Budget, pi agent loop.
 * Baseline parity is by construction: the seed agent makes one runAgentNode
 * call with the parity seed's exact parameters — the identical code path a
 * workflow ctx.agent() takes.
 *
 * Agent module contract:
 *   export async function solve(task, core) -> answer (JSON-serializable)
 *   core = {
 *     runAgentNode(prompt, opts, seq, core.deps),  // the node primitive (agent-node.ts)
 *     deps,          // frozen AgentNodeDeps: models, tools, journal, budget, streamFn, signal
 *     pi,            // @earendil-works/pi-agent-core module — for candidates that rewrite loop internals
 *   }
 *
 * Deviation from the workflow arm, on purpose: no sandbox. Free-code evolution
 * is the point of this baseline (meta-harness runs arbitrary Python); the
 * cheating boundaries (no outside information sources, no bypassing token
 * accounting) are enforced by the frozen client/accounting plus audit of the
 * append-only agents/ archive, not by a VM.
 */
function loadDotEnv(path: string): void {
	if (!existsSync(path)) return;
	for (const line of readFileSync(path, "utf8").split("\n")) {
		const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$/);
		if (m && process.env[m[1]] === undefined && m[2] !== "") process.env[m[1]] = m[2];
	}
}

async function main(): Promise<number> {
	const { values } = parseArgs({
		options: {
			agent: { type: "string" },
			task: { type: "string" },
			out: { type: "string" },
			domain: { type: "string", default: "none" },
			models: { type: "string" },
			"max-tokens": { type: "string", default: "400000" },
			"max-wallclock-sec": { type: "string", default: "1800" },
			"validate-agent": { type: "string" },
		},
	});

	// validation gate: the module must import and export solve()
	if (values["validate-agent"]) {
		try {
			const mod = await import(pathToFileURL(resolve(values["validate-agent"])).href);
			if (typeof mod.solve !== "function") throw new Error("module does not export solve()");
			console.log(`OK ${values["validate-agent"]}`);
			return 0;
		} catch (err) {
			console.error(`INVALID: ${String(err)}`);
			return 1;
		}
	}

	if (!values.agent || !values.task || !values.out) {
		console.error("usage: run-meta.ts --agent agent.mjs --task task.json --out dir [--domain d]");
		return 2;
	}

	const rootDir = resolve(import.meta.dirname, "../..");
	loadDotEnv(join(rootDir, ".env"));
	const modelsPath = values.models ?? join(rootDir, "configs/models.yaml");

	let task: TaskPayload;
	let journal: Journal;
	let models: ReturnType<typeof loadModels>;
	let tools: ReturnType<typeof buildRegistry>;
	try {
		task = JSON.parse(readFileSync(values.task, "utf8")) as TaskPayload;
		journal = new Journal(values.out);
		models = loadModels(modelsPath);
		tools = buildRegistry(values.domain ?? "none", task);
	} catch (err) {
		console.error(`infra error: ${String(err)}`);
		return 2;
	}

	// Same SUT boundary as run.ts: candidates never see the _meta model tier.
	const visibleModels = new Map([...models].filter(([, entry]) => entry.sut));
	if (visibleModels.size === 0) {
		console.error("infra error: no models marked sut: true");
		return 2;
	}

	const budget = new Budget(Number(values["max-tokens"]), Number(values["max-wallclock-sec"]) * 1000);
	const controller = new AbortController();
	const killTimer = setTimeout(() => controller.abort(), budget.remainingMs());

	const finish = (status: string, result: unknown, error?: string): number => {
		clearTimeout(killTimer);
		const summary = {
			status,
			result: result ?? null,
			totalTokens: budget.spentSplit(),
			durationMs: budget.elapsedMs(),
			...(error ? { error } : {}),
		};
		journal.write({ type: "workflow_end", ...summary, result: preview(result) });
		writeFileSync(join(values.out!, "result.json"), JSON.stringify(summary, null, 2));
		return 0;
	};

	let solve: (task: TaskPayload, core: unknown) => Promise<unknown>;
	try {
		const mod = await import(pathToFileURL(resolve(values.agent)).href);
		if (typeof mod.solve !== "function") throw new Error("module does not export solve()");
		solve = mod.solve;
	} catch (err) {
		journal.write({ type: "workflow_start", task_id: task.id, workflow_name: null, domain: values.domain });
		return finish("workflow_error", null, `load: ${String(err)}`);
	}

	journal.write({
		type: "workflow_start",
		task_id: task.id,
		workflow_name: values.agent,
		domain: values.domain,
		models_available: [...visibleModels.keys()],
	});

	const deps: AgentNodeDeps = {
		models: visibleModels,
		tools,
		journal,
		budget,
		streamFn: withTransportRetry(streamSimple as StreamFn, journal),
		signal: controller.signal,
	};
	const core = { runAgentNode, deps, pi: piAgentCore };

	try {
		const result = await solve(task, core);
		if (budget.exceeded()) return finish(budget.elapsedMs() >= budget.maxWallclockMs ? "timeout" : "budget_exceeded", result);
		return finish("ok", result);
	} catch (err) {
		if (budget.exceeded()) {
			return finish(budget.elapsedMs() >= budget.maxWallclockMs ? "timeout" : "budget_exceeded", null, String(err));
		}
		return finish("workflow_error", null, String(err));
	}
}

main().then((code) => process.exit(code));
