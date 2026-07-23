import { execFile } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Type } from "@earendil-works/pi-ai";
import type { AgentTool } from "@earendil-works/pi-agent-core";

const OUTPUT_LIMIT = 16 * 1024;
// 15s, down from 120s. In the realmath baseline the eight rollouts that died on the
// wall clock spent 74% of it inside this tool (longest single call 120s), while the 146
// that finished spent 3% and never exceeded 2s. A long call is therefore not a hard
// computation making progress — it is sympy stuck on something it will not finish, and
// the rollout is better served by being told so while it still has turns left.
const TIMEOUT_MS = 15_000;

function truncate(s: string): string {
	return s.length > OUTPUT_LIMIT ? `${s.slice(0, OUTPUT_LIMIT)}\n…[truncated ${s.length - OUTPUT_LIMIT} bytes]` : s;
}

/**
 * Sandboxed Python execution for the realmath domain: each call runs `python3 -c <code>`
 * in a throwaway working directory with a hard timeout. sympy is expected in the
 * interpreter given via OWF_PYTHON (default python3).
 */
export function makePythonTool(): AgentTool<any> {
	const workdir = mkdtempSync(join(tmpdir(), "owf-py-"));
	const interpreter = process.env.OWF_PYTHON ?? "python3";
	return {
		name: "python",
		label: "Python",
		description:
			"Execute a self-contained Python script and return its stdout/stderr. sympy is available. State does NOT persist between calls; print() what you need to see.",
		parameters: Type.Object({
			code: Type.String({ description: "Python source to execute" }),
		}),
		execute: async (_id, params, signal) => {
			const { code } = params as { code: string };
			return new Promise((resolve) => {
				const child = execFile(
					interpreter,
					["-c", code],
					{ cwd: workdir, timeout: TIMEOUT_MS, maxBuffer: 8 * 1024 * 1024, env: { PATH: process.env.PATH ?? "", HOME: workdir } },
					(err, stdout, stderr) => {
						const timedOut = Boolean(err && (err as { killed?: boolean }).killed);
						const parts: string[] = [];
						if (stdout) parts.push(truncate(stdout));
						if (stderr) parts.push(`[stderr]\n${truncate(stderr)}`);
						// Say so explicitly, and say it even when the script printed something first:
						// a silent kill reads as "no output" and invites a retry of the same dead end.
						if (timedOut) {
							parts.push(
								`[timeout] killed after ${TIMEOUT_MS / 1000}s. This computation will not finish as written — ` +
									"do not retry it unchanged. Narrow it (smaller case, closed form, a different sympy entry point) or reason it out directly.",
							);
						} else if (err && !stdout && !stderr) {
							parts.push(`[error] ${err.message}`);
						}
						const exitCode = err && typeof (err as { code?: unknown }).code === "number" ? (err as { code: number }).code : err ? 1 : 0;
						resolve({
							content: [{ type: "text", text: parts.join("\n") || "(no output)" }],
							details: { exitCode, timedOut },
						});
					},
				);
				signal?.addEventListener("abort", () => child.kill("SIGKILL"), { once: true });
			});
		},
	};
}
