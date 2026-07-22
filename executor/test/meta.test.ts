import assert from "node:assert/strict";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { makeMetaTools } from "../src/tools/meta.js";
import type { TaskPayload } from "../src/types.js";

function setup(): { tools: ReturnType<typeof makeMetaTools>; optRoot: string; wfDir: string; candidate: string } {
	const base = mkdtempSync(join(tmpdir(), "owf-meta-"));
	const optRoot = join(base, "opt");
	const wfDir = join(base, "workflows");
	mkdirSync(join(optRoot, "iter_001"), { recursive: true });
	mkdirSync(wfDir, { recursive: true });
	writeFileSync(join(optRoot, "evidence.json"), '{"hello":1}');
	writeFileSync(join(base, "secret-gold.json"), '{"gold":"42"}'); // outside scope
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
	return { tools: makeMetaTools(task), optRoot, wfDir, candidate };
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
