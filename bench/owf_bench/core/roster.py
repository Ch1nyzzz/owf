"""Generate the meta agent's playbook from the lab ledgers (pure function).

The meta agent never reads task_book.json / attribution.json — those are
search-side ledgers. This module distills them into lab/playbook.md: slot
shelves with measured effects and confidence labels, named presets (the
proven members re-expressed as assembly specs), the dispatch policy, budgets
and boundaries. Regenerate after every search phase; never hand-edit.

Confidence labels are mechanical: `clean` when a single-difference pair
contrast exists, `confounded` when the component only appears in bundles,
`k=1` when the carriers' evidence on most tasks is a single rep.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

LOW_MULT_SIG = 6  # a task this rare (<= solvers) qualifies as a preset's signature case

# Presets are the book's members, dispatched as their ORIGINAL workflow files —
# the ledger's evidence was produced by those exact files, so dispatching them
# sidesteps assembler fidelity entirely. The retro specs below remain only as
# documentation of the v8 decomposition (assemble.py's novel path uses their
# vocabulary); they are NOT the dispatch mechanism.
V8_RETRO_SPECS: dict[str, dict] = {
    "iter_001": {"prompt": "evidence_lead", "prompt_variant": "final_line", "decoding": None, "turn_budget": 64,
                 "cutoff_turn": 56, "closure": {"type": "post_editor", "window": 8000, "maxTurns": 3},
                 "output": "raw", "verifier": None, "monitor": None},
    "iter_003": {"prompt": "evidence_lead", "prompt_variant": "one_line_only", "decoding": None, "turn_budget": 64,
                 "cutoff_turn": 56, "closure": {"type": "post_editor", "window": 8000, "maxTurns": 1},
                 "output": "raw", "verifier": None, "monitor": None},
    "iter_002": {"prompt": "bounded_researcher", "decoding": "greedy_nothink", "turn_budget": 64,
                 "cutoff_turn": 56, "closure": None, "output": "schema_direct", "verifier": None, "monitor": None},
    "iter_005": {"prompt": "evidence_lead", "decoding": None, "turn_budget": 62, "cutoff_turn": 56,
                 "closure": None, "output": "regex_extractor", "verifier": None, "monitor": None},
    "iter_006": {"prompt": "evidence_lead", "decoding": None, "turn_budget": 60, "cutoff_turn": 56,
                 "closure": None, "output": "regex_extractor", "verifier": "exact_name", "monitor": None},
    "iter_007": {"prompt": "evidence_lead", "decoding": "greedy_nothink", "turn_budget": 60, "cutoff_turn": 56,
                 "closure": {"type": "inhook_editor", "window": 16000, "maxTurns": 2},
                 "output": "regex_extractor", "verifier": None, "monitor": None},
    "iter_008": {"prompt": "evidence_lead", "decoding": "greedy_nothink", "turn_budget": 60, "cutoff_turn": 56,
                 "closure": {"type": "inhook_editor", "window": 16000, "maxTurns": 2},
                 "output": "regex_extractor", "verifier": None, "monitor": "starvation_close"},
    "iter_009": {"prompt": "evidence_lead", "decoding": "greedy_nothink", "turn_budget": 58, "cutoff_turn": 56,
                 "closure": {"type": "inhook_editor", "window": 10000, "maxTurns": 1},
                 "output": "regex_extractor", "verifier": None, "monitor": None},
    "iter_010": {"prompt": "evidence_lead", "decoding": "greedy_nothink", "turn_budget": 56, "cutoff_turn": 52,
                 "closure": {"type": "inhook_editor", "window": 16000, "maxTurns": 3},
                 "output": "regex_extractor", "verifier": None, "monitor": None},
    "seed":     {"prompt": "seed_persistent", "decoding": None, "turn_budget": 64, "cutoff_turn": None,
                 "closure": None, "output": "raw", "verifier": None, "monitor": None},
}
def preset_workflows(book: dict) -> dict[str, str]:
    """member -> original workflow file (the artifact the book's evidence measured)."""
    return {name: meta["workflow"] for name, meta in book["members"].items()}


def preset_stats(book: dict) -> dict[str, dict]:
    stats: dict[str, dict] = {}
    for name in book["members"]:
        owned = [t for t, rec in book["tasks"].items() if rec["owner"] == name]
        entries = [rec["entries"][name] for rec in book["tasks"].values() if name in rec["entries"]]
        solved = [e for e in entries if e["passes"] > 0]
        confirmed = [e for e in entries if e["status"] == "confirmed"]
        toks = [e["mean_tokens"] for e in entries]
        stats[name] = {
            "owned": owned,
            "solved_any": len(solved),
            "confirmed": len(confirmed),
            "mean_tokens": round(sum(toks) / len(toks)) if toks else None,
        }
    return stats


def member_highlights(lab: dict, member: str) -> str:
    comps = [cid for cid, on in lab.get("members", {}).get(member, {}).items() if on]
    key = [c for c in comps if c.split(".")[0] in ("topology", "closure", "prompt")][:3]
    return ", ".join(key) if key else "seed baseline"


def load_instructions(domain: str) -> dict[str, str]:
    tasks_file = Path(__file__).resolve().parents[3] / "data" / domain / "tasks.jsonl"
    if not tasks_file.exists():
        return {}
    out = {}
    for line in tasks_file.read_text().splitlines():
        if line.strip():
            t = json.loads(line)
            out[t["id"]] = t.get("instruction", "")
    return out


def confidence(comp: str, attr: dict) -> str:
    if comp in attr.get("single_diff_contrasts", {}):
        return "clean"
    for line in attr.get("confounds", []):
        if comp in line:
            return "confounded"
    return "k=1"


def render_playbook(book: dict, lab: dict, attr: dict, doctrine: str = "free") -> str:
    cov = book["coverage"]
    mult = attr["multiplicity"]
    stats = preset_stats(book)
    n_members = len(book["member_order"])
    # A preset's signature cases are the hard tasks it reproducibly owns. One
    # case is an anecdote, not a specialty: the held-out exam's only losing
    # dispatch pocket (iter_003, 0/2) came from generalising a single signature
    # case into a whole question style. Presets below this bar are marked thin
    # and the dispatch policy restricts them to near-verbatim matches.
    sig_cases = {
        name: sorted((t for t in st["owned"] if 0 < mult.get(t, 0) <= LOW_MULT_SIG),
                     key=lambda t: mult.get(t, 0))[:3]
        for name, st in stats.items()
    }
    thin = {name for name, cases in sig_cases.items() if len(cases) < 2}
    lines: list[str] = []
    a = lines.append

    a("# Lab playbook — bcplus (auto-generated; regenerate, never edit)")
    a("")
    a("You are the dispatcher of a workflow lab for corpus-grounded multi-hop questions.")
    a("Given one question, output an assembly spec (or preset) for the workflow that will answer it.")
    a("Every number below is measured on a 50-task training distribution; citations are task ids.")
    a("")
    a("## 1. Triage — two regimes")
    a("")
    n_easy = sum(1 for v in mult.values() if v >= 9)
    n_hard = sum(1 for v in mult.values() if 0 < v <= 5)
    a(f"- Of solved tasks, {n_easy} are solved by nearly every assembly (>=9/{n_members}) — for these, ANY")
    a("  reliable preset works; cost is the tiebreaker, not a reason to skip judgement.")
    a(f"- {n_hard} tasks are solved by few assemblies (<=5/{n_members}) — only here does component choice matter.")
    a("- Hard-task signals: many interlocking clues with no rare anchor phrase; person-name questions")
    a("  demanding full/formal names; questions whose decisive evidence needs long verification chains.")
    a(f"- {len(cov['unsolved'])} training tasks were solved by NO assembly ({', '.join(cov['unsolved'])}).")
    a("  If a question resembles these (compound rare-entity + relation chains), still answer — but")
    a("  prefer a cheap preset; extra spend did not help on this cluster.")
    a("")
    a("## 2. Slot shelves (measured component effects)")
    a("")
    a("Effect = mean pass-rate delta of assemblies carrying the component vs not (train, all/hard tasks).")
    a("Confidence: clean = isolated by a single-difference pair; confounded = only observed in bundles;")
    a("k=1 = thin evidence. Treat non-clean numbers as directional hints, not facts.")
    a("")
    a("| component | slot | effect all | effect hard | confidence | note |")
    a("|---|---|---|---|---|---|")
    comps = attr["components"]
    for comp, s in sorted(comps.items(), key=lambda kv: -(kv[1]["mean_delta_all"] or 0)):
        meta = lab["components"].get(comp, {})
        fmt = lambda v: f"{v:+.3f}" if v is not None else "n/a"
        a(f"| {comp} | {meta.get('slot','?')} | {fmt(s['mean_delta_all'])} | {fmt(s['mean_delta_low_mult'])} "
          f"| {confidence(comp, attr)} | {meta.get('desc','')[:90]} |")
    a("")
    a("Param sensitivity is real: assemblies identical in components but differing in closure window/turns")
    a("flipped up to 14 tasks against each other. Prefer proven param values from the presets below.")
    a("")
    a("## 3. Presets (proven assemblies, ranked by confirmed holdings)")
    a("")
    a("| preset | owned tasks | confirmed solves | mean tokens/task | key components |")
    a("|---|---|---|---|---|")
    for name, st in sorted(stats.items(), key=lambda kv: -len(kv[1]["owned"])):
        a(f"| {name} | {len(st['owned'])} | {st['confirmed']} | {st['mean_tokens']} | {member_highlights(lab, name)} |")
    a("")
    a("### Signature cases — match a new question against these before dispatching")
    a("")
    a("The questions below are the HARD tasks (few assemblies solve them) that each preset")
    a("reproducibly owns. A signature MATCH means NEAR-VERBATIM correspondence with a cited")
    a("case — same entity type, same answer shape, same clue structure. That signal outranks")
    a("the default chain. Stylistic or genre resemblance is NOT a match, for ANY preset:")
    a("style-matched dispatches measured net-negative against the generalist; near-verbatim")
    a("ones measured net-positive. When unsure, the question goes to the generalist.")
    a("")
    a("A preset with a SINGLE signature case is additionally marked [thin evidence]: one case")
    a("is an anecdote, not a specialty — apply the near-verbatim bar with extra suspicion.")
    a("")
    instructions = load_instructions(book["domain"] or "")
    for name, st in sorted(stats.items(), key=lambda kv: -len(kv[1]["owned"])):
        tag = " [thin evidence]" if name in thin and sig_cases[name] else ""
        for t in sig_cases[name]:
            snippet = " ".join(instructions.get(t, "").split())[:220]
            if snippet:
                a(f"- **{name}**{tag} (solved by {mult[t]}/{n_members}) — {t}: “{snippet}…”")
    a("")
    a("## 4. Dispatch policy")
    a("")
    chain = [c["member"] for c in book["cover_set"]]
    generalist = chain[0] if chain else "seed"
    if doctrine == "conservative":
        a(f"1. DEFAULT: preset `{generalist}`. Escalation chain when a task looks hard for it: "
          f"{' -> '.join(chain)} (the minimal set that covers every solved training task).")
        a("2. A signature-case match (section 3) outranks the default chain: dispatch that owner.")
        a("   A match is NEAR-VERBATIM only (section 3's bar) — style or genre resemblance stays")
        a("   with the default chain, no matter how suggestive.")
        a("3. Prefer the cheaper of two presets with equal claim; never leave a question unanswered.")
        a(f"4. Output format: JSON: {{\"preset\": \"{generalist}\", \"reason\": \"...\"}} — a preset name from "
          "section 3 (only emit an {\"assembly\": ...} object if the interface explicitly allows it).")
        a("")
    else:
        a("Judge EVERY question on its own evidence and pick the preset with the strongest claim —")
        a("there is no default reflex. Weigh, in order of evidential strength:")
        a("1. Signature-case match (section 3): a question in NEAR-VERBATIM correspondence with a")
        a("   preset's owned hard case is that preset's to take. Style or genre resemblance is not")
        a("   a match for any preset — such questions are judged on rules 2-3 instead.")
        a("2. Preset records (section 3 table): owned tasks, confirmed solves, and key components —")
        a("   match the question's demands (answer shape, verification depth, anchor rarity) to what")
        a("   a preset's components are built for.")
        a(f"3. `{generalist}` is the proven generalist with the widest confirmed record: choose it when no")
        a("   specialist has a STRONGER claim — as a judgement, not a habit. When two presets have equal")
        a("   claim, take the cheaper one. Never leave a question unanswered.")
        a("4. Output format: JSON: {\"preset\": \"<name>\", \"reason\": \"...\"} — any preset from section 3")
        a("   (only emit an {\"assembly\": ...} object if the interface you were given explicitly allows it).")
        a("")
    a("## 5. Boundaries and warnings")
    a("")
    a(f"- Lab record on training: union {cov['union']}/{cov['universe']}, reproducibly confirmed "
      f"{cov['confirmed']}/{cov['universe']}.")
    a("- Never leave a question unanswered: every preset enforces answer-before-exhaustion; keep that.")
    a("- Effects marked confounded/k=1 come from single runs; do not chase them into exotic assemblies.")
    a("- temperature-0 decoding measured NEGATIVE on hard tasks (confounded) — when in doubt, omit")
    a("  `decoding` rather than forcing greedy.")
    return "\n".join(lines) + "\n"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--opt-root", required=True)
    p.add_argument("--doctrine", choices=["free", "conservative"], default="free")
    p.add_argument("--out", help="default: <opt-root>/lab/playbook.md")
    args = p.parse_args()
    opt_root = Path(args.opt_root).resolve()
    book = json.loads((opt_root / "task_book.json").read_text())
    lab = json.loads((opt_root / "lab/components.json").read_text())
    attr = json.loads((opt_root / "lab/attribution.json").read_text())
    dest = Path(args.out) if args.out else opt_root / "lab/playbook.md"
    dest.write_text(render_playbook(book, lab, attr, args.doctrine))
    print(f"wrote {dest} ({dest.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
