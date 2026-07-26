"""Replay a run's history to rebuild its Pareto frontier under the current SCORE_BAND.

Needed because the frontier in state.json is not raw data — it is the accumulated result of
per-round admission decisions, so changing the admission rule invalidates it. When the noise
band went to 0 (see optimize.py:SCORE_BAND), realmath's frontier had already lost round 2's
0.680 @ 146,698 to round 4's 0.640 @ 131,842: a 0.040 gap read as a tie, then decided on
tokens. Nothing in the loop can recover an evicted point, so it has to be replayed in.

The replay is exact rather than approximate: every round's measured score and token count is
in history, and the candidate/report paths are derived from the iteration number, so the
rebuilt frontier is what the driver would have produced had it always used this rule.

`entered_frontier` and `frontier_after` are rewritten too. They are not decoration — the
stagnation predicate counts rounds with no frontier entry, so leaving stale values would
make the watchdog reason about admissions that never happened under the current rule.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from owf_bench.core.optimize import SCORE_BAND, best_by_score, update_frontier

ROOT = Path(__file__).resolve().parents[3]


def seed_point(opt_root: Path, domain: str) -> dict:
    """Rebuild the baseline point. It may have been evicted, so it cannot be read back
    from the stored frontier — it is reconstructed from the baseline report itself."""
    report = json.loads((opt_root / "evidence/baseline/report.json").read_text())
    tokens = report.get("tokens_per_task_total")
    if tokens is None:
        t = report["tokens_per_task"]
        tokens = t["input"] + t["output"]
    return {
        "workflow": str(ROOT / f"workflows/{domain}/seed_parity.js"),
        "score": report["score"],
        "tokens": tokens,
        "tokens_per_task": report["tokens_per_task"],
        "report": str(opt_root / "evidence/baseline/report.json"),
    }


def replay(opt_root: Path, state: dict) -> tuple[list[dict], list[str]]:
    frontier = [seed_point(opt_root, state["domain"])]
    log: list[str] = []
    for rec in state["history"]:
        if rec.get("candidate_score") is None:
            continue
        it = rec["iter"]
        point = {
            "workflow": str(opt_root / f"iter_{it:03d}/candidate.js"),
            "score": rec["candidate_score"],
            "tokens": rec["candidate_tokens"],
            "tokens_per_task": rec.get("candidate_tokens_per_task"),
            "report": str(opt_root / f"iter_{it:03d}/eval/report.json"),
        }
        frontier, entered = update_frontier(frontier, point, SCORE_BAND)
        was = rec.get("entered_frontier")
        if was != entered:
            log.append(f"  iter{it}: entered_frontier {was} -> {entered}")
        rec["entered_frontier"] = entered
        best = best_by_score(frontier)
        rec["frontier_after"] = {"points": len(frontier), "best_score": best["score"], "best_tokens": best["tokens"]}
    return frontier, log


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--opt-root", required=True)
    p.add_argument("--apply", action="store_true", help="write state.json (default: dry run)")
    args = p.parse_args()

    opt_root = Path(args.opt_root).resolve()
    state_path = opt_root / "state.json"
    state = json.loads(state_path.read_text())

    old = state["frontier"]
    new, log = replay(opt_root, state)

    # The stored band may have been floored by the old `max(band, 0.02)`, which existed to
    # keep a tiny band from making admission too strict. Nothing is gated on it now — it is
    # shown to the optimizer as evidence about score stability — so a floored value is just
    # a wrong number in its prompt. Restore the measurement.
    measured = json.loads((opt_root / "evidence/stability.json").read_text())["noise_band"]
    if state.get("noise_band") != measured:
        log.append(f"  noise_band {state.get('noise_band')} -> {measured} (measured; no longer gates admission)")
        state["noise_band"] = measured

    print(f"{opt_root.name}  (SCORE_BAND={SCORE_BAND}, measured noise_band={state.get('noise_band')})")
    print(f"  before: {len(old)} pts")
    for pt in old:
        print(f"    {pt['score']:.4f} @ {pt['tokens']:,}")
    print(f"  after:  {len(new)} pts")
    for pt in new:
        print(f"    {pt['score']:.4f} @ {pt['tokens']:,}  {Path(pt['workflow']).parent.name}/{Path(pt['workflow']).name}")
    recovered = [pt for pt in new if not any(abs(pt["score"] - o["score"]) < 1e-9 and pt["tokens"] == o["tokens"] for o in old)]
    for pt in recovered:
        print(f"  RECOVERED: {pt['score']:.4f} @ {pt['tokens']:,} — evicted under the old band")
    print("\n".join(log) if log else "  (no entered_frontier flags changed)")

    if not args.apply:
        print("\n  dry run — pass --apply to write")
        return
    state["frontier"] = new
    backup = state_path.with_suffix(".json.preband0.bak")
    if not backup.exists():
        shutil.copy(state_path, backup)
        print(f"  original state kept at {backup.name}")
    state_path.write_text(json.dumps(state, indent=1))
    print(f"  wrote {state_path}")


if __name__ == "__main__":
    main()
