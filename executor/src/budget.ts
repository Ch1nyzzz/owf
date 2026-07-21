import type { Usage } from "@earendil-works/pi-ai";

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
