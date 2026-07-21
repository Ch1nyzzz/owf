import { Type } from "@earendil-works/pi-ai";
import type { AgentTool } from "@earendil-works/pi-agent-core";
import type { ToolRegistry } from "../types.js";

/** bcplus: fixed-corpus retrieval via the local server (bench/owf_bench/bcplus/server.py). */
export function makeRetrievalTools(): ToolRegistry {
	const base = process.env.OWF_BCPLUS_SERVER ?? "http://127.0.0.1:8931";

	const search: AgentTool<any> = {
		name: "search",
		label: "Corpus search",
		description: "Keyword-search the fixed document corpus (BM25). Returns docids with titles and snippets; read full documents with open_doc.",
		parameters: Type.Object({ query: Type.String(), k: Type.Optional(Type.Number()) }),
		execute: async (_id, params) => {
			const { query, k } = params as { query: string; k?: number };
			const resp = await fetch(`${base}/search?q=${encodeURIComponent(query)}&k=${Math.min(k ?? 8, 20)}`);
			if (!resp.ok) throw new Error(`search server ${resp.status}`);
			const rows = (await resp.json()) as Array<{ docid: string; title: string; snippet: string }>;
			const text = rows.map((r, i) => `${i + 1}. [${r.docid}] ${r.title}\n   ${r.snippet.replace(/\s+/g, " ").slice(0, 300)}`).join("\n");
			return { content: [{ type: "text", text: text || "(no results)" }], details: {} };
		},
	};

	const openDoc: AgentTool<any> = {
		name: "open_doc",
		label: "Open document",
		description: "Read a corpus document in full by its docid.",
		parameters: Type.Object({ docid: Type.String() }),
		execute: async (_id, params) => {
			const { docid } = params as { docid: string };
			const resp = await fetch(`${base}/doc?id=${encodeURIComponent(docid)}`);
			const data = (await resp.json()) as { text?: string; url?: string; error?: string };
			if (data.error) return { content: [{ type: "text", text: data.error }], details: {} };
			return { content: [{ type: "text", text: `URL: ${data.url}\n\n${data.text}` }], details: {} };
		},
	};

	return { search, open_doc: openDoc };
}
