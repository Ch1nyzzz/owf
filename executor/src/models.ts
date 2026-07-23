import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { parse } from "yaml";
import type { Model } from "@earendil-works/pi-ai";
import * as codexCatalog from "@earendil-works/pi-ai/providers/openai-codex.models";
import type { ModelEntry } from "./types.js";

/** Read the Codex CLI OAuth access token (refreshed externally by the codex CLI). */
function codexAccessToken(): string | undefined {
	try {
		const auth = JSON.parse(readFileSync(join(homedir(), ".codex/auth.json"), "utf8"));
		return auth?.tokens?.access_token;
	} catch {
		return undefined;
	}
}

/** Substitute ${VAR} / ${VAR:-default} with process.env values. */
function substEnv(value: string): string {
	return value.replace(/\$\{([A-Z0-9_]+)(?::-([^}]*))?\}/g, (_, name, fallback) => process.env[name] ?? fallback ?? "");
}

interface RawModel {
	base_url?: string;
	model_id?: string;
	api_key_env?: string;
	context_window?: number;
	max_tokens?: number;
	reasoning?: boolean;
	cost?: { input?: number; output?: number; cache_read?: number };
	/** Use a pi-ai provider catalog entry verbatim, e.g. "openai-codex/gpt-5.6-terra". */
	catalog?: string;
	/** Auth mode: "codex" reads the Codex CLI OAuth token per call. */
	auth?: string;
}

export function loadModels(configPath: string): Map<string, ModelEntry> {
	const raw = parse(readFileSync(configPath, "utf8")) as { models: Record<string, RawModel> };
	const out = new Map<string, ModelEntry>();
	for (const [key, m] of Object.entries(raw.models)) {
		if (m.catalog) {
			const [provider, id] = m.catalog.split("/", 2);
			if (provider !== "openai-codex") throw new Error(`unsupported catalog provider: ${provider}`);
			const cat = (codexCatalog as Record<string, Record<string, Model<any>>>).OPENAI_CODEX_MODELS ?? Object.values(codexCatalog)[0];
			const model = (cat as Record<string, Model<any>>)[id];
			if (!model) throw new Error(`catalog model not found: ${m.catalog}`);
			const resolveKey = m.auth === "codex" ? codexAccessToken : () => (m.api_key_env ? process.env[m.api_key_env] : undefined);
			out.set(key, { key, model, apiKey: resolveKey(), resolveKey });
			continue;
		}
		if (!m.base_url || !m.model_id || !m.api_key_env) throw new Error(`model ${key}: base_url, model_id, api_key_env required`);
		const model: Model<"openai-completions"> = {
			id: substEnv(m.model_id!),
			name: key,
			api: "openai-completions",
			provider: "openai-compatible",
			baseUrl: substEnv(m.base_url!),
			reasoning: m.reasoning ?? false,
			input: ["text"],
			cost: {
				input: m.cost?.input ?? 0,
				output: m.cost?.output ?? 0,
				cacheRead: m.cost?.cache_read ?? 0,
				cacheWrite: 0,
			},
			contextWindow: m.context_window ?? 131072,
			maxTokens: m.max_tokens ?? 8192,
		};
		out.set(key, { key, model, apiKey: process.env[m.api_key_env!] });
	}
	return out;
}
