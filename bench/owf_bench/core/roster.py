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

# The retro assembly spec of each member (mirrors lab/components.json).
PRESETS: dict[str, dict] = {
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
# iter_004 (scout->resolver topology) is not expressible by the v1 assembler;
# it stays available only as a whole-file preset.
FILE_PRESETS = {"iter_004": "iter_004/candidate.js"}


def preset_stats(book: dict) -> dict[str, dict]:
    stats: dict[str, dict] = {}
    for name in list(PRESETS) + list(FILE_PRESETS):
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


def render_playbook(book: dict, lab: dict, attr: dict) -> str:
    cov = book["coverage"]
    mult = attr["multiplicity"]
    stats = preset_stats(book)
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
    a(f"- Of solved tasks, {n_easy} are solved by nearly every assembly (>=9/11) — for these, ANY")
    a("  reliable preset works and CHEAPEST WINS. Do not overbuild.")
    a(f"- {n_hard} tasks are solved by few assemblies (<=5/11) — only here does component choice matter.")
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
    a("| preset | owned tasks | confirmed solves | mean tokens/task | spec highlights |")
    a("|---|---|---|---|---|")
    for name, st in sorted(stats.items(), key=lambda kv: -len(kv[1]["owned"])):
        spec = PRESETS.get(name)
        hi = "scout->resolver two-stage (file preset)" if name in FILE_PRESETS else \
             f"{spec['prompt']}, cutoff {spec['cutoff_turn']}, closure {spec['closure']['type'] if spec['closure'] else 'none'}, out {spec['output']}"
        a(f"| {name} | {len(st['owned'])} | {st['confirmed']} | {st['mean_tokens']} | {hi} |")
    a("")
    a("### Signature cases — match a new question against these before dispatching")
    a("")
    a("The questions below are the HARD tasks (few assemblies solve them) that each preset")
    a("reproducibly owns. If a new question resembles one in style and structure, that owner")
    a("is the highest-probability dispatch — this signal outranks the default chain.")
    a("")
    instructions = load_instructions(book["domain"] or "")
    for name, st in sorted(stats.items(), key=lambda kv: -len(kv[1]["owned"])):
        hard_owned = sorted((t for t in st["owned"] if 0 < mult.get(t, 0) <= LOW_MULT_SIG),
                            key=lambda t: mult.get(t, 0))[:3]
        for t in hard_owned:
            snippet = " ".join(instructions.get(t, "").split())[:220]
            if snippet:
                a(f"- **{name}** (solved by {mult[t]}/11) — {t}: “{snippet}…”")
    a("")
    a("## 4. Dispatch policy")
    a("")
    a("1. DEFAULT: preset `iter_001` (largest confirmed coverage). Escalation chain when a task looks")
    a("   hard for it: iter_001 -> iter_003 -> iter_009 -> seed (the minimal set that covers every")
    a("   solved training task).")
    a("2. Person-name questions asking full/real/baptismal names: consider `verifier: exact_name`.")
    a("3. Compose a NOVEL assembly only when triage finds no preset whose record covers this task type.")
    a("   Novel specs must reuse proven param values; validation will reject contract violations, and an")
    a("   invalid spec falls back to the default preset.")
    a("4. Output format: JSON, either {\"preset\": \"iter_001\", \"reason\": \"...\"} or")
    a("   {\"assembly\": {<spec per the schema you were given>}, \"reason\": \"...\"}.")
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
    args = p.parse_args()
    opt_root = Path(args.opt_root).resolve()
    book = json.loads((opt_root / "task_book.json").read_text())
    lab = json.loads((opt_root / "lab/components.json").read_text())
    attr = json.loads((opt_root / "lab/attribution.json").read_text())
    dest = opt_root / "lab/playbook.md"
    dest.write_text(render_playbook(book, lab, attr))
    print(f"wrote {dest} ({dest.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
