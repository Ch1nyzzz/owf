import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { parseArgs } from "node:util";
import { streamSimple } from "@earendil-works/pi-ai/compat";
import type { StreamFn } from "@earendil-works/pi-agent-core";
import { Budget } from "./budget.js";
import { BudgetExceededError, buildCtx } from "./dsl.js";
import { Journal, preview } from "./journal.js";
import { loadModels } from "./models.js";
import { loadWorkflow } from "./sandbox.js";
import { buildRegistry } from "./tools/registry.js";
import type { TaskPayload } from "./types.js";

/** Load KEY=VALUE lines from an .env file without overriding existing env. */
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
			workflow: { type: "string" },
			task: { type: "string" },
			out: { type: "string" },
			domain: { type: "string", default: "none" },
			models: { type: "string" },
			"max-tokens": { type: "string", default: "400000" },
			"max-wallclock-sec": { type: "string", default: "1800" },
			"validate-only": { type: "string" },
		},
	});

	// validation gate for external callers (watchdog rewrites, CI): parse + shape only
	if (values["validate-only"]) {
		try {
			const wf = loadWorkflow(values["validate-only"]);
			console.log(`OK ${wf.meta.name}`);
			return 0;
		} catch (err) {
			console.error(`INVALID: ${String(err)}`);
			return 1;
		}
	}

	if (!values.workflow || !values.task || !values.out) {
		console.error("usage: run.ts --workflow wf.js --task task.json --out dir [--domain d] [--models models.yaml]");
		return 2; // infra error: bad invocation
	}

	const rootDir = resolve(import.meta.dirname, "../..");
	loadDotEnv(join(rootDir, ".env"));
	const modelsPath = values.models ?? join(rootDir, "configs/models.yaml");

	// Harness-level setup faults are infra errors (DSL §9): non-zero exit, no journal verdict.
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
		return 0; // candidate failures are verdicts, not process errors
	};

	let wf;
	try {
		wf = loadWorkflow(values.workflow);
	} catch (err) {
		journal.write({ type: "workflow_start", task_id: task.id, workflow_name: null, domain: values.domain });
		return finish("workflow_error", null, `load: ${String(err)}`);
	}

	journal.write({
		type: "workflow_start",
		task_id: task.id,
		workflow_name: wf.meta.name,
		domain: values.domain,
		models_available: [...models.keys()],
	});

	const ctx = buildCtx({ task, models, tools, journal, budget, streamFn: streamSimple as StreamFn, signal: controller.signal });

	try {
		const result = await wf.run(ctx);
		if (budget.exceeded()) return finish(budget.elapsedMs() >= budget.maxWallclockMs ? "timeout" : "budget_exceeded", result);
		return finish("ok", result);
	} catch (err) {
		if (err instanceof BudgetExceededError || budget.exceeded()) {
			return finish(budget.elapsedMs() >= budget.maxWallclockMs ? "timeout" : "budget_exceeded", null, String(err));
		}
		return finish("workflow_error", null, String(err));
	}
}

main().then((code) => process.exit(code));
