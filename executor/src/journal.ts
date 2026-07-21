import { appendFileSync, mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const PREVIEW_LIMIT = 2048;

export function preview(value: unknown): string {
	let s: string;
	if (typeof value === "string") s = value;
	else {
		try {
			s = JSON.stringify(value);
		} catch {
			s = String(value);
		}
	}
	if (s === undefined || s === null) return "";
	return s.length > PREVIEW_LIMIT ? `${s.slice(0, PREVIEW_LIMIT)}…[+${s.length - PREVIEW_LIMIT}]` : s;
}

/**
 * Append-only journal (DSL §8). One JSON object per line in journal.jsonl;
 * full per-node transcripts as node-<seq>-<label>.jsonl next to it.
 */
export class Journal {
	private path: string;
	constructor(private outDir: string) {
		mkdirSync(outDir, { recursive: true });
		this.path = join(outDir, "journal.jsonl");
		writeFileSync(this.path, "");
	}

	write(event: Record<string, unknown>): void {
		appendFileSync(this.path, `${JSON.stringify({ ts: Date.now(), ...event })}\n`);
	}

	nodeTranscript(seq: number, label: string, messages: unknown[]): void {
		const safe = label.replace(/[^a-zA-Z0-9_-]/g, "_").slice(0, 60);
		const file = join(this.outDir, `node-${seq}-${safe}.jsonl`);
		writeFileSync(file, messages.map((m) => JSON.stringify(m)).join("\n") + "\n");
	}
}
