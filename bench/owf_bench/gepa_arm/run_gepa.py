"""CLI for the GEPA prompt-only baseline arm.

Budget parity with the main arm is expressed in metric calls (one call = one
task rollout): a 10-round main run spends ~10 full 50-task evals plus probes,
so --max-metric-calls defaults to 600.

Run detached via scripts/launch_gepa.sh, which sources .env first (solver and
judge keys) — same discipline as launch_opt.sh.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import gepa

from owf_bench.gepa_arm.adapter import SEED_PROMPTS, OwfGEPAAdapter
from owf_bench.gepa_arm.reflect_codex import CodexReflectionLM


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--domain", required=True, choices=sorted(SEED_PROMPTS))
    p.add_argument("--out", required=True)
    p.add_argument("--max-metric-calls", type=int, default=600)
    p.add_argument("--reflection-minibatch", type=int, default=3)
    p.add_argument("--workers", type=int, default=16)
    p.add_argument("--max-tokens", type=int, default=600_000)
    p.add_argument("--max-sec", type=int, default=1800)
    p.add_argument("--reflection-model", default="gpt-5.6-terra")
    p.add_argument("--limit", type=int, help="cap trainset size (smoke runs only)")
    p.add_argument("--no-skip-perfect", action="store_true",
                   help="reflect even on all-correct minibatches (smoke runs: tiny trainsets hit them constantly)")
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args()

    out_root = Path(args.out).resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / "config.json").write_text(json.dumps(vars(args), indent=1, default=str))

    adapter = OwfGEPAAdapter(domain=args.domain, out_root=out_root, max_tokens=args.max_tokens,
                             max_sec=args.max_sec, workers=args.workers, limit=args.limit)
    reflection_lm = CodexReflectionLM(log_dir=out_root / "reflection", model=args.reflection_model)

    print(f"GEPA {args.domain}: {len(adapter.trainset)} train tasks, budget {args.max_metric_calls} metric calls")
    result = gepa.optimize(
        seed_candidate={"system_prompt": SEED_PROMPTS[args.domain]},
        trainset=adapter.trainset,
        adapter=adapter,
        reflection_lm=reflection_lm,
        max_metric_calls=args.max_metric_calls,
        reflection_minibatch_size=args.reflection_minibatch,
        skip_perfect_score=not args.no_skip_perfect,
        run_dir=str(out_root / "gepa_state"),
        seed=args.seed,
        raise_on_exception=False,
        display_progress_bar=False,
    )

    best = result.best_candidate
    (out_root / "best_candidate.json").write_text(json.dumps(best, indent=1, ensure_ascii=False))
    best_wf = adapter._write_candidate(best["system_prompt"])
    summary = {
        "best_workflow": str(best_wf),
        "reflection_calls": reflection_lm.calls,
        "metric_calls_used": adapter._call_seq,
    }
    for attr in ("val_aggregate_scores", "best_idx", "total_metric_calls"):
        value = getattr(result, attr, None)
        if value is not None:
            summary[attr] = value
    (out_root / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    print(json.dumps(summary, indent=1, default=str))
    print(f"best candidate workflow: {best_wf}")
    print("NOTE: the champion still owes k=3 confirmation + held-out test, same as the main arm.")


if __name__ == "__main__":
    main()
