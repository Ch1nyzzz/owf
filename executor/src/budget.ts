import type { Usage } from "@earendil-works/pi-ai";

/**
 * Full prompt tokens for one call, cache hits included.
 *
 * pi subtracts cache reads/writes from `usage.input`, so `input` alone is only the
 * cache-MISS part and is therefore provider-dependent. gpugeek reports `cached_tokens`
 * erratically — a cold call claimed 6656 hits and the warm repeat of the same request
 * reported none — which would swing the recorded input for one identical call between
 * 167 and 6823. We deliberately price cache hits at the full input rate, so counting
 * them here makes the number stable no matter what a provider claims about its cache.
 */
export function promptTokens(usage: Usage | undefined): number {
	if (!usage) return 0;
	return (usage.input ?? 0) + (usage.cacheRead ?? 0) + (usage.cacheWrite ?? 0);
}

/** Per-task budget (DSL §6): token ceiling + wall clock ceiling. */
export class Budget {
	private spentInput = 0;
	private spentOutput = 0;
	private readonly start = Date.now();

	constructor(
		readonly maxTokens: number,
		readonly maxWallclockMs: number,
	) {}

	addUsage(usage: Usage | undefined): void {
		if (!usage) return;
		this.spentInput += promptTokens(usage);
		this.spentOutput += usage.output ?? 0;
	}

	spent(): number {
		return this.spentInput + this.spentOutput;
	}

	spentSplit(): { input: number; output: number } {
		return { input: this.spentInput, output: this.spentOutput };
	}

	elapsedMs(): number {
		return Date.now() - this.start;
	}

	remainingTokens(): number {
		return Math.max(0, this.maxTokens - this.spent());
	}

	remainingMs(): number {
		return Math.max(0, this.maxWallclockMs - this.elapsedMs());
	}

	exceeded(): boolean {
		return this.spent() >= this.maxTokens || this.elapsedMs() >= this.maxWallclockMs;
	}
}
