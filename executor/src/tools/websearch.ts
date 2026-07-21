import { Type } from "@earendil-works/pi-ai";
import type { AgentTool } from "@earendil-works/pi-agent-core";
import type { ToolRegistry } from "../types.js";

const FETCH_LIMIT = 24 * 1024;

/** web_search: Tavily first, Serper fallback (same provider-chain design as
 * WorldCalib's GAIA tools, rewritten in TS). url_fetch: page -> readable text. */
export function makeWebSearchTools(): ToolRegistry {
	const webSearch: AgentTool<any> = {
		name: "web_search",
		label: "Web search",
		description: "Search the web. Returns titles, URLs and snippets. Use url_fetch to read a result in full.",
		parameters: Type.Object({ query: Type.String(), max_results: Type.Optional(Type.Number()) }),
		execute: async (_id, params) => {
			const { query, max_results } = params as { query: string; max_results?: number };
			const k = Math.min(max_results ?? 8, 15);
			let text: string;
			if (process.env.TAVILY_API_KEY) {
				const resp = await fetch("https://api.tavily.com/search", {
					method: "POST",
					headers: { "Content-Type": "application/json" },
					body: JSON.stringify({ api_key: process.env.TAVILY_API_KEY, query, max_results: k, include_answer: false }),
				});
				if (!resp.ok) throw new Error(`tavily ${resp.status}: ${(await resp.text()).slice(0, 200)}`);
				const data = (await resp.json()) as { results: Array<{ title: string; url: string; content: string }> };
				text = data.results.map((r, i) => `${i + 1}. ${r.title}\n   ${r.url}\n   ${r.content.slice(0, 400)}`).join("\n");
			} else if (process.env.SERPER_API_KEY) {
				const resp = await fetch("https://google.serper.dev/search", {
					method: "POST",
					headers: { "X-API-KEY": process.env.SERPER_API_KEY, "Content-Type": "application/json" },
					body: JSON.stringify({ q: query, num: k }),
				});
				if (!resp.ok) throw new Error(`serper ${resp.status}`);
				const data = (await resp.json()) as { organic?: Array<{ title: string; link: string; snippet?: string }> };
				text = (data.organic ?? []).map((r, i) => `${i + 1}. ${r.title}\n   ${r.link}\n   ${r.snippet ?? ""}`).join("\n");
			} else {
				throw new Error("no search provider configured (TAVILY_API_KEY / SERPER_API_KEY)");
			}
			return { content: [{ type: "text", text: text || "(no results)" }], details: {} };
		},
	};

	const urlFetch: AgentTool<any> = {
		name: "url_fetch",
		label: "Fetch URL",
		description: "Fetch a web page and return its visible text (truncated).",
		parameters: Type.Object({ url: Type.String() }),
		execute: async (_id, params, signal) => {
			const { url } = params as { url: string };
			const resp = await fetch(url, { signal: signal ?? AbortSignal.timeout(30_000), headers: { "User-Agent": "Mozilla/5.0 (owf-agent)" } });
			const raw = await resp.text();
			const text = raw
				.replace(/<script[\s\S]*?<\/script>/gi, "")
				.replace(/<style[\s\S]*?<\/style>/gi, "")
				.replace(/<[^>]+>/g, " ")
				.replace(/&[a-z#0-9]+;/gi, " ")
				.replace(/[ \t]+/g, " ")
				.replace(/\n\s*\n+/g, "\n")
				.trim();
			const clipped = text.length > FETCH_LIMIT ? `${text.slice(0, FETCH_LIMIT)}\n…[truncated]` : text;
			return { content: [{ type: "text", text: `[${resp.status}] ${clipped || "(empty)"}` }], details: { status: resp.status } };
		},
	};

	return { web_search: webSearch, url_fetch: urlFetch };
}
