import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";
import type { StreamFn } from "@earendil-works/pi-agent-core";
import type { Journal } from "./journal.js";

const MAX_ATTEMPTS = 4;
const BACKOFF_MS = [2_000, 5_000, 10_000];

/**
 * Transport-level retry around the model stream call.
 *
 * gpugeek intermittently answers a well-formed request with `400 (no body)`
 * under load; pi converts that into an assistant message with stopReason
 * "error", the agent loop stops, and the whole node dies on one bad HTTP
 * response (2026-07-29: 45/50 exam rollouts lost this way). A call that
 * failed before producing ANY text is pure transport noise, so it is retried
 * with backoff; once a call has streamed content, the error is forwarded
 * unchanged (retrying would duplicate partial output). Errored attempts
 * report no usage, so discarding them does not skew the token axis.
 *
 * Each attempt is fully buffered before its verdict is known. The executor is
 * headless — nothing consumes deltas live — so buffering costs latency only
 * in the retry case, exactly when latency no longer matters.
 */
export function withTransportRetry(streamFn: StreamFn, journal: Journal, backoffMs: number[] = BACKOFF_MS): StreamFn {
	return async (model, context, options) => {
		for (let attempt = 1; ; attempt++) {
			const inner = await streamFn(model, context, options);
			const events: unknown[] = [];
			for await (const ev of inner) events.push(ev);
			const message = await inner.result();

			const producedText = (message.content ?? []).some(
				(c) => (c as { type?: string; text?: string }).type === "text" && (c as { text?: string }).text,
			);
			const transient = message.stopReason === "error" && !producedText;

			if (!transient || attempt >= MAX_ATTEMPTS) {
				const out = createAssistantMessageEventStream();
				for (const ev of events) out.push(ev as never);
				out.end(message);
				return out;
			}

			journal.write({
				type: "transport_retry",
				attempt,
				model: (model as { id?: string }).id ?? String(model),
				error: String((message as { errorMessage?: unknown }).errorMessage ?? "").slice(0, 200),
			});
			const backoff = backoffMs[Math.min(attempt - 1, backoffMs.length - 1)] + Math.floor(Math.random() * (backoffMs[0] ? 1_000 : 0));
			await new Promise((resolve) => setTimeout(resolve, backoff));
		}
	};
}
