import assert from "node:assert/strict";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { makeMetaTools } from "../src/tools/meta.js";
import type { TaskPayload } from "../src/types.js";

function setup(): { tools: ReturnType<typeof makeMetaTools>; optRoot: string; wfDir: string; candidate: string; baselineDir: string } {
	const base = mkdtempSync(join(tmpdir(), "owf-meta-"));
	const optRoot = join(base, "opt");
	const wfDir = join(base, "workflows");
	mkdirSync(join(optRoot, "iter_001"), { recursive: true });
	mkdirSync(wfDir, { recursive: true });
	writeFileSync(join(optRoot, "evidence.json"), '{"hello":1}');
	writeFileSync(join(base, "secret-gold.json"), '{"gold":"42"}'); // outside scope
	// A baseline run is a sibling of the opt root; the driver links it into evidence/
	// so the optimizer can read its journals. Both forms are exercised below.
	const baselineDir = join(base, "baseline_run");
	mkdirSync(join(baselineDir, "task-1__r0"), { recursive: true });
	writeFileSync(join(baselineDir, "report.json"), '{"score":0.42}');
	writeFileSync(join(baselineDir, "task-1__r0/journal.jsonl"), '{"type":"node_end"}\n');
	mkdirSync(join(optRoot, "evidence"), { recursive: true });
	symlinkSync(baselineDir, join(optRoot, "evidence/baseline"));
	const candidate = join(optRoot, "iter_001/candidate.js");
	const task: TaskPayload = {
		id: "t",
		instruction: "x",
		opt_root: optRoot,
		workflows_dir: wfDir,
		candidate_path: candidate,
		bench_root: join(base, "bench"),
		domain: "realmath",
	};
	return { tools: makeMetaTools(task), optRoot, wfDir, candidate, baselineDir };
}

const textOf = (r: { content: Array<{ type: string; text?: string }> }): string =>
	r.content.filter((c) => c.type === "text").map((c) => c.text).join("");

test("read_file: in-scope reads work, out-of-scope throws", async () => {
	const { tools, optRoot } = setup();
	const ok = await tools.read_file.execute("1", { path: join(optRoot, "evidence.json") });
	assert.ok(textOf(ok).includes('"hello"'));
	await assert.rejects(
		() => tools.read_file.execute("2", { path: join(optRoot, "../secret-gold.json") }),
		/out of scope/,
	);
	await assert.rejects(() => tools.read_file.execute("3", { path: "/etc/passwd" }), /out of scope/);
});

test("baseline evidence linked into the opt root is readable; the raw sibling is not", async () => {
	const { tools, optRoot, baselineDir } = setup();
	// Scoping must not resolve symlinks, or the driver's evidence/baseline link silently
	// stops working and the optimizer loses every baseline rollout journal.
	const report = await tools.read_file.execute("1", { path: join(optRoot, "evidence/baseline/report.json") });
	assert.ok(textOf(report).includes("0.42"));
	const journal = await tools.read_file.execute("2", { path: join(optRoot, "evidence/baseline/task-1__r0/journal.jsonl") });
	assert.ok(textOf(journal).includes("node_end"));
	const listing = await tools.list_dir.execute("3", { path: join(optRoot, "evidence/baseline") });
	assert.ok(textOf(listing).includes("task-1__r0"));
	// Reaching the same run by its real path stays refused: the link widens evidence, not scope.
	await assert.rejects(() => tools.read_file.execute("4", { path: join(baselineDir, "report.json") }), /out of scope/);
});

test("write_workflow: invalid content rejected without landing; valid content lands", async () => {
	const { tools, candidate } = setup();
	const bad = await tools.write_workflow.execute("1", { content: "export const meta = {}\n// no run" });
	assert.ok(textOf(bad).includes("VALIDATION FAILED"));
	assert.ok(!existsSync(candidate));
	const good = await tools.write_workflow.execute("2", {
		content: "export const meta = { name: 'c1' }\nexport default async function run(ctx) { return 1 }\n",
	});
	assert.ok(textOf(good).includes("written and validated"));
	assert.ok(existsSync(candidate));
	assert.ok(readFileSync(candidate, "utf8").includes("c1"));
});

test("write_notes lands in opt root", async () => {
	const { tools, optRoot } = setup();
	await tools.write_notes.execute("1", { content: "# beliefs\n- none yet" });
	assert.equal(readFileSync(join(optRoot, "NOTES.md"), "utf8"), "# beliefs\n- none yet");
});
