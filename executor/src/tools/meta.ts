import { execFile } from "node:child_process";
import { existsSync, readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { Type } from "@earendil-works/pi-ai";
import type { AgentTool } from "@earendil-works/pi-agent-core";
import { loadWorkflow } from "../sandbox.js";
import type { TaskPayload, ToolRegistry } from "../types.js";

/**
 * Privileged tools for the _meta domain (optimizer / watchdog workflows).
 *
 * Boundary is mechanical, not advisory: reads are confined to the optimization
 * run root and the workflows/ tree; the only writes are the designated candidate
 * file (behind a validation gate) and the optimizer's own NOTES.md. Gold answers
 * live under data/ which is outside the read scope by construction.
 */

const OUT_LIMIT = 48 * 1024;

function clip(s: string): string {
	return s.length > OUT_LIMIT ? `${s.slice(0, OUT_LIMIT)}\n…[truncated ${s.length - OUT_LIMIT} bytes; re-read with a narrower target]` : s;
}

function text(t: string) {
	return { content: [{ type: "text" as const, text: t }], details: {} };
}

export function makeMetaTools(task: TaskPayload): ToolRegistry {
	const optRoot = resolve(String(task.opt_root ?? ""));
	const workflowsDir = resolve(String(task.workflows_dir ?? ""));
	const candidatePath = resolve(String(task.candidate_path ?? ""));
	const notesPath = resolve(optRoot, "NOTES.md");
	const benchRoot = resolve(String(task.bench_root ?? ""));
	if (!optRoot || !workflowsDir) throw new Error("_meta task must carry opt_root and workflows_dir");

	const inScope = (p: string): string => {
		const r = resolve(p);
		if (r.startsWith(optRoot + "/") || r === optRoot || r.startsWith(workflowsDir + "/") || r === workflowsDir) return r;
		throw new Error(`path out of scope: ${p} (readable: ${optRoot}, ${workflowsDir})`);
	};

	const read_file: AgentTool<any> = {
		name: "read_file",
		label: "Read file",
		description: `Read a file. Scope: the optimization run root (${optRoot}: evidence, eval reports, journals, notes) and the workflows tree (${workflowsDir}).`,
		parameters: Type.Object({ path: Type.String() }),
		execute: async (_id, params) => {
			const p = inScope((params as { path: string }).path);
			if (!existsSync(p)) return text(`(not found: ${p})`);
			return text(clip(readFileSync(p, "utf8")));
		},
	};

	const list_dir: AgentTool<any> = {
		name: "list_dir",
		label: "List directory",
		description: "List a directory within scope (name, size, kind).",
		parameters: Type.Object({ path: Type.String() }),
		execute: async (_id, params) => {
			const p = inScope((params as { path: string }).path);
			if (!existsSync(p)) return text(`(not found: ${p})`);
			const rows = readdirSync(p).map((n) => {
				const st = statSync(resolve(p, n));
				return `${st.isDirectory() ? "d" : "f"} ${st.size}\t${n}`;
			});
			return text(clip(rows.join("\n") || "(empty)"));
		},
	};

	const write_workflow: AgentTool<any> = {
		name: "write_workflow",
		label: "Write candidate workflow",
		description:
			"Write the candidate workflow.js. The content is validated (parse + module shape) before landing; on failure nothing is written and the error is returned — fix and retry. The destination path is fixed by the harness.",
		parameters: Type.Object({ content: Type.String() }),
		execute: async (_id, params) => {
			const { content } = params as { content: string };
			const tmp = `${candidatePath}.staging`;
			writeFileSync(tmp, content);
			try {
				loadWorkflow(tmp); // static gate: parse, meta literal, run function, bans
			} catch (err) {
				return { content: [{ type: "text" as const, text: `VALIDATION FAILED, nothing written: ${String(err)}` }], details: {}, };
			}
			writeFileSync(candidatePath, content);
			return text(`written and validated: ${candidatePath}`);
		},
	};

	const write_notes: AgentTool<any> = {
		name: "write_notes",
		label: "Write notes",
		description: "Replace NOTES.md (your persistent memory across optimization rounds). Keep it curated — you will re-read it cold next round.",
		parameters: Type.Object({ content: Type.String() }),
		execute: async (_id, params) => {
			writeFileSync(notesPath, (params as { content: string }).content);
			return text(`notes saved (${(params as { content: string }).content.length} bytes)`);
		},
	};

	const run_probe: AgentTool<any> = {
		name: "run_probe",
		label: "Run probe evaluation",
		description:
			"Evaluate the current candidate workflow on a small task sample. Caps: limit<=12, repeats<=2. Returns the score report. Slow (minutes) — use deliberately.",
		parameters: Type.Object({
			limit: Type.Number({ description: "number of tasks (<=12)" }),
			repeats: Type.Number({ description: "repeats per task (<=2)" }),
			subset: Type.String({ description: "'train' only", default: "train" }),
		}),
		executionMode: "sequential",
		execute: async (_id, params, signal) => {
			const limit = Math.min(Math.max(1, Number((params as any).limit) || 5), 12);
			const repeats = Math.min(Math.max(1, Number((params as any).repeats) || 1), 2);
			if (!existsSync(candidatePath)) return text("no candidate written yet — call write_workflow first");
			const probeDir = resolve(optRoot, `probe_${Date.now()}`);
			return new Promise((resolvePromise) => {
				const child = execFile(
					"python3",
					[
						resolve(benchRoot, "owf_bench/core/runner.py"),
						"--domain", String(task.domain),
						"--workflow", candidatePath,
						"--subset", "train",
						"--limit", String(limit),
						"--repeats", String(repeats),
						"--workers", String(Math.min(limit * repeats, 16)),
						"--out", probeDir,
						"--max-tokens", "300000",
						"--max-wallclock-sec", "1500",
					],
					{ cwd: benchRoot ? resolve(benchRoot, "..") : undefined, env: { ...process.env, PYTHONPATH: benchRoot }, timeout: 3_000_000, maxBuffer: 16 * 1024 * 1024 },
					(err, stdout, stderr) => {
						const reportPath = resolve(probeDir, "report.json");
						if (existsSync(reportPath)) {
							resolvePromise(text(`probe done (${probeDir}):\n${readFileSync(reportPath, "utf8")}`));
						} else {
							resolvePromise(text(`probe failed: ${String(err ?? "no report")}\n${clip(stderr || stdout || "")}`));
						}
					},
				);
				signal?.addEventListener("abort", () => child.kill("SIGKILL"), { once: true });
			});
		},
	};

	return { read_file, list_dir, write_workflow, write_notes, run_probe };
}
