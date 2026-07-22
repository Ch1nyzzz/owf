import type { TaskPayload, ToolRegistry } from "../types.js";
import { makeMetaTools } from "./meta.js";
import { makePythonTool } from "./python.js";
import { makeWebSearchTools } from "./websearch.js";
import { makeRetrievalTools } from "./retrieval.js";

/**
 * Harness-owned tool registry (DSL §7). Workflows select tools by name; the executor
 * exposes exactly the set for the requested domain.
 *
 * terminal (tb2, M3) / search + open_doc (bcplus, M4) / web_search + url_fetch (finsearch, M5)
 * are added with their milestones.
 */
export function buildRegistry(domain: string, task?: TaskPayload): ToolRegistry {
	switch (domain) {
		case "realmath":
			return { python: makePythonTool() };
		case "finsearch":
			return { ...makeWebSearchTools(), python: makePythonTool() };
		case "bcplus":
			return makeRetrievalTools();
		case "_meta":
			if (!task) throw new Error("_meta domain requires the task payload");
			return makeMetaTools(task);
		case "none":
			return {};
		default:
			throw new Error(`unknown domain: ${domain}`);
	}
}
