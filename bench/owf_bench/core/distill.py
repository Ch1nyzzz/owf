"""Distill stage A: component x task attribution from the confirmed task book.

The member x component matrix (lab/components.json, hand-audited) turns the
run's per-task grades into a free factorial experiment: every member is a point
in assembly space, so a component's effect on a task is estimable by comparing
members that carry it against members that do not — no new rollouts.

Three estimators, from weakest to strongest:
  * with_vs_without: mean pass rate delta across all members (biased by every
    other component the carriers share — read together with `confounds`);
  * single_diff_pairs: member pairs whose component sets differ by EXACTLY one
    component give a clean paired contrast for that component;
  * param_pairs: pairs with identical component sets but different params
    (e.g. iter_007 vs iter_009/010: closure window/turn variants).

Informativeness weighting: a task every member solves says nothing about any
component. Summaries are split by solver multiplicity — the low-multiplicity
regime (<= LOW_MULT solvers) is where specialisation signal lives.

Outcomes come from task_book.json entries (pass_rate over all graded reps);
judge_error/infra rows were already excluded upstream. Non-owner entries are
mostly k=1 (the confirmation protocol tests owners only) — deltas are noisy at
per-task level, which is why summaries aggregate and per-task values carry the
evidence counts.
"""

from __future__ import annotations

import argparse
import json
from itertools import combinations
from pathlib import Path

LOW_MULT = 5   # <= this many solvers: the specialisation regime
HIGH_MULT = 9  # >= this many: the everyone-solves regime


def load(opt_root: Path) -> tuple[dict, dict]:
    book = json.loads((opt_root / "task_book.json").read_text())
    lab = json.loads((opt_root / "lab/components.json").read_text())
    return book, lab


def outcomes(book: dict) -> dict[str, dict[str, float]]:
    """task -> member -> pass_rate (graded members only)."""
    return {
        tid: {m: e["pass_rate"] for m, e in rec["entries"].items()}
        for tid, rec in book["tasks"].items()
    }


def multiplicity(book: dict) -> dict[str, int]:
    return {tid: sum(1 for e in rec["entries"].values() if e["passes"] > 0)
            for tid, rec in book["tasks"].items()}


def with_vs_without(lab: dict, out: dict) -> dict[str, dict]:
    members = lab["members"]
    result: dict[str, dict] = {}
    for comp in lab["components"]:
        carriers = {m for m, comps in members.items() if comps.get(comp)}
        others = set(members) - carriers
        per_task = {}
        for tid, scores in out.items():
            w = [scores[m] for m in carriers if m in scores]
            wo = [scores[m] for m in others if m in scores]
            if w and wo:
                per_task[tid] = {"delta": round(sum(w) / len(w) - sum(wo) / len(wo), 4),
                                 "n_with": len(w), "n_without": len(wo)}
        result[comp] = {"carriers": sorted(carriers), "per_task": per_task}
    return result


def single_diff_pairs(lab: dict) -> dict[str, list[tuple[str, str]]]:
    """component -> [(member_without, member_with)] where the pair differs by that one component."""
    members = {m: {c for c, v in comps.items() if v} for m, comps in lab["members"].items()}
    pairs: dict[str, list[tuple[str, str]]] = {}
    for a, b in combinations(sorted(members), 2):
        diff = members[a] ^ members[b]
        if len(diff) == 1:
            comp = next(iter(diff))
            lo, hi = (a, b) if comp in members[b] else (b, a)
            pairs.setdefault(comp, []).append((lo, hi))
    return pairs


def param_pairs(lab: dict) -> list[tuple[str, str]]:
    """pairs with identical component sets (their contrast is purely parametric)."""
    members = {m: frozenset(c for c, v in comps.items() if v) for m, comps in lab["members"].items()}
    return [(a, b) for a, b in combinations(sorted(members), 2) if members[a] == members[b]]


def pair_contrast(pair: tuple[str, str], out: dict) -> dict:
    """per-task paired delta (with - without) plus win/loss/tie counts."""
    without, with_ = pair
    deltas = {}
    wins = losses = 0
    for tid, scores in out.items():
        if without in scores and with_ in scores:
            d = scores[with_] - scores[without]
            if d:
                deltas[tid] = round(d, 4)
                wins += d > 0
                losses += d < 0
    return {"pair": [without, with_], "wins": wins, "losses": losses, "flips": deltas}


def summarize_component(comp: str, data: dict, mult: dict) -> dict:
    per_task = data["per_task"]
    lows = [d["delta"] for t, d in per_task.items() if mult.get(t, 0) <= LOW_MULT and mult.get(t, 0) > 0]
    highs = [d["delta"] for t, d in per_task.items() if mult.get(t, 0) >= HIGH_MULT]
    alls = [d["delta"] for d in per_task.values()]
    top = sorted(per_task.items(), key=lambda kv: -abs(kv[1]["delta"]))[:5]
    return {
        "carriers": data["carriers"],
        "mean_delta_all": round(sum(alls) / len(alls), 4) if alls else None,
        "mean_delta_low_mult": round(sum(lows) / len(lows), 4) if lows else None,
        "mean_delta_high_mult": round(sum(highs) / len(highs), 4) if highs else None,
        "top_tasks": {t: d["delta"] for t, d in top},
    }


def build_attribution(opt_root: Path) -> dict:
    book, lab = load(opt_root)
    out = outcomes(book)
    mult = multiplicity(book)
    wvw = with_vs_without(lab, out)
    sdp = single_diff_pairs(lab)
    return {
        "opt_root": str(opt_root),
        "params": {"low_mult": LOW_MULT, "high_mult": HIGH_MULT},
        "multiplicity": mult,
        "components": {c: summarize_component(c, d, mult) for c, d in wvw.items()},
        "per_task": {c: d["per_task"] for c, d in wvw.items()},
        "single_diff_contrasts": {
            comp: [pair_contrast(p, out) for p in pairs] for comp, pairs in sdp.items()
        },
        "param_contrasts": [pair_contrast(p, out) for p in param_pairs(lab)],
        "confounds": lab.get("confounds", []),
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--opt-root", required=True)
    p.add_argument("--apply", action="store_true", help="write <opt-root>/lab/attribution.json")
    args = p.parse_args()
    opt_root = Path(args.opt_root).resolve()
    attr = build_attribution(opt_root)

    print(f"components ({len(attr['components'])}), tasks ({len(attr['multiplicity'])})")
    print(f"{'component':28s} {'mean(all)':>9s} {'low-mult':>9s} {'high-mult':>9s}  carriers")
    for comp, s in sorted(attr["components"].items(), key=lambda kv: -(kv[1]["mean_delta_all"] or 0)):
        fmt = lambda v: f"{v:+.3f}" if v is not None else "  n/a"
        print(f"{comp:28s} {fmt(s['mean_delta_all']):>9s} {fmt(s['mean_delta_low_mult']):>9s} "
              f"{fmt(s['mean_delta_high_mult']):>9s}  {len(s['carriers'])}")
    print("\nsingle-difference contrasts (cleanest evidence):")
    for comp, contrasts in attr["single_diff_contrasts"].items():
        for c in contrasts:
            print(f"  {comp:28s} {c['pair'][0]} -> {c['pair'][1]}: +{c['wins']} / -{c['losses']} "
                  f"{c['flips'] if len(c['flips']) <= 6 else ''}")
    print("\nparam-only contrasts (same components, different settings):")
    for c in attr["param_contrasts"]:
        print(f"  {c['pair'][0]} vs {c['pair'][1]}: +{c['wins']} / -{c['losses']}")

    if args.apply:
        dest = opt_root / "lab/attribution.json"
        dest.write_text(json.dumps(attr, indent=1, ensure_ascii=False))
        print(f"\nwrote {dest}")


if __name__ == "__main__":
    main()
