import type { Usage } from "@earendil-works/pi-ai";

/** Per-task budget (DSL §6): token ceiling + wall clock ceiling.
 *
 * Input counts cache-MISS tokens only — pi already subtracts `cacheRead`/`cacheWrite`
 * from `usage.input`, and we keep it that way. Probing gpugeek directly (20 warm calls
 * on a cold nonce) it does cache and does report hits, consistently at the same value,
 * but omits the field on about 10% of calls; an omission inflates that call's recorded
 * input rather than shrinking it, so the error is one-directional and averages into a
 * stable multiplier across the hundreds of calls in a rollout. We measure tokens rather
 * than money precisely because gpugeek documents neither the cache field nor a cache
 * price — its published billing has a single input rate — so any CNY figure would rest
 * on an assumption we cannot source.
 */
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
		this.spentInput += usage.input ?? 0;
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
