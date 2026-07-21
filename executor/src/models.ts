import { readFileSync } from "node:fs";
import { parse } from "yaml";
import type { Model } from "@earendil-works/pi-ai";
import type { ModelEntry } from "./types.js";

/** Substitute ${VAR} / ${VAR:-default} with process.env values. */
function substEnv(value: string): string {
	return value.replace(/\$\{([A-Z0-9_]+)(?::-([^}]*))?\}/g, (_, name, fallback) => process.env[name] ?? fallback ?? "");
}

interface RawModel {
	base_url: string;
	model_id: string;
	api_key_env: string;
	context_window?: number;
	max_tokens?: number;
	reasoning?: boolean;
	cost?: { input?: number; output?: number };
}

export function loadModels(configPath: string): Map<string, ModelEntry> {
	const raw = parse(readFileSync(configPath, "utf8")) as { models: Record<string, RawModel> };
	const out = new Map<string, ModelEntry>();
	for (const [key, m] of Object.entries(raw.models)) {
		const model: Model<"openai-completions"> = {
			id: substEnv(m.model_id),
			name: key,
			api: "openai-completions",
			provider: "openai-compatible",
			baseUrl: substEnv(m.base_url),
			reasoning: m.reasoning ?? false,
			input: ["text"],
			cost: {
				input: m.cost?.input ?? 0,
				output: m.cost?.output ?? 0,
				cacheRead: 0,
				cacheWrite: 0,
			},
			contextWindow: m.context_window ?? 131072,
			maxTokens: m.max_tokens ?? 8192,
		};
		out.set(key, { key, model, apiKey: process.env[m.api_key_env] });
	}
	return out;
}
