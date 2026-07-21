import type { ToolRegistry } from "../types.js";
import { makePythonTool } from "./python.js";
import { makeWebSearchTools } from "./websearch.js";

/**
 * Harness-owned tool registry (DSL §7). Workflows select tools by name; the executor
 * exposes exactly the set for the requested domain.
 *
 * terminal (tb2, M3) / search + open_doc (bcplus, M4) / web_search + url_fetch (finsearch, M5)
 * are added with their milestones.
 */
export function buildRegistry(domain: string): ToolRegistry {
	switch (domain) {
		case "realmath":
			return { python: makePythonTool() };
		case "finsearch":
			return { ...makeWebSearchTools(), python: makePythonTool() };
		case "none":
			return {};
		default:
			throw new Error(`unknown domain: ${domain}`);
	}
}
