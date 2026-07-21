import { readFileSync } from "node:fs";
import vm from "node:vm";

export interface LoadedWorkflow {
	meta: { name: string; version?: number };
	run: (ctx: unknown) => Promise<unknown>;
}

function bannedFn(name: string): () => never {
	return () => {
		throw new Error(`${name} is banned inside workflows (determinism contract, DSL §6)`);
	};
}

/**
 * Load a workflow.js module (DSL §1) into a locked-down vm context.
 *
 * The module shape is fixed (`export const meta = {...}` + `export default async function`),
 * so a two-substitution source transform lets us run it as a plain vm.Script without
 * experimental ESM-in-vm flags.
 */
export function loadWorkflow(path: string): LoadedWorkflow {
	const source = readFileSync(path, "utf8");
	const transformed = source.replace(/\bexport\s+const\s+meta\s*=/, "__wf.meta =").replace(/\bexport\s+default\b/, "__wf.run =");
	if (!transformed.includes("__wf.meta") || !transformed.includes("__wf.run")) {
		throw new Error("workflow must contain `export const meta = {...}` and `export default async function (ctx) {...}`");
	}

	const wf: Partial<LoadedWorkflow> = {};
	const bannedDate = new Proxy(Date, {
		construct(target, args: unknown[]) {
			if (args.length === 0) throw new Error("argless `new Date()` is banned inside workflows (DSL §6)");
			return new (target as DateConstructor)(...(args as [number]));
		},
		get(target, prop) {
			if (prop === "now") return bannedFn("Date.now");
			return Reflect.get(target, prop);
		},
	});
	const bannedMath = new Proxy(Math, {
		get(target, prop) {
			if (prop === "random") return bannedFn("Math.random");
			return Reflect.get(target, prop);
		},
	});

	const context = vm.createContext(
		{
			__wf: wf,
			Date: bannedDate,
			Math: bannedMath,
			setTimeout: bannedFn("setTimeout"),
			setInterval: bannedFn("setInterval"),
			fetch: bannedFn("fetch"),
			process: undefined,
			require: bannedFn("require"),
			console: undefined,
		},
		{ codeGeneration: { strings: false, wasm: false } },
	);

	const script = new vm.Script(transformed, { filename: path });
	script.runInContext(context, { timeout: 5000 });

	if (!wf.meta || typeof wf.meta !== "object" || typeof (wf.meta as { name?: unknown }).name !== "string") {
		throw new Error("workflow meta must be an object literal with a string `name`");
	}
	if (typeof wf.run !== "function") {
		throw new Error("workflow default export must be a function");
	}
	return wf as LoadedWorkflow;
}
