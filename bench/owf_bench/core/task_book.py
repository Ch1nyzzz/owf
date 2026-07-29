"""Per-task champion book: which lab member solves each task, at what cost.

The champion-replacement paradigm collapses per-task information into one
aggregate score — a candidate that loses on the average is discarded whole,
along with every task only it could solve. The book keeps the per-task ledger:
for every task, every member ever evaluated on it (contenders), the current
owner under a lexicographic rule (strength first, then cheaper with an
anti-churn margin), and the ownership history. Coverage — how many tasks at
least one member solves — is the search loop's objective; the book is its
ledger and the raw material later distilled into roster cards. The meta agent
never reads this file.

The book is a pure function of the graded evidence on disk: it is rebuilt in
full from results.jsonl files (evidence/baseline, iter_*/eval, confirm/*) on
every update, so rebuilding a historical run and updating a live one are the
same code path and the result is idempotent. Scores exist only in
results.jsonl — rollout dirs alone cannot be graded offline (grading needs
the judge) — so results.jsonl is the sole source of truth.

Statistical discipline: a solve observed at k=1 is `provisional`; an owner is
`confirmed` only at >=CONFIRM_REPS repeats with pass rate >=CONFIRM_RATE.
Coverage is reported on both levels (union = solved at least once anywhere;
confirmed = reproducibly solved), and admission decisions downstream should
trust only confirmed. Targeted confirmation runs go to confirm/<member>/ —
NEVER point the runner back at a historical eval dir: it rewrites report.json
and results.jsonl at --out, destroying the evidence this book is built from.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

TOKEN_MARGIN = 0.10   # a same-strength challenger must be >=10% cheaper to take a task
CONFIRM_REPS = 2      # min repeats before a solve counts as confirmed
CONFIRM_RATE = 2 / 3  # min pass rate at those repeats

STATUS_RANK = {"confirmed": 2, "provisional": 1, "fail": 0}


def collect_samples(source_dir: Path) -> dict[str, list[dict]]:
    """Graded (score, tokens, rollout) samples per task from one runner output dir.

    judge_error rows are NOT evidence: the judge never delivered a verdict, so the
    rep says nothing about the member. Counting them as failures let a dead judge
    (2026-07-29: official-API balance exhausted, every call 402) drag pass rates
    down and churn 40 task ownerships on garbage. Infra errors are excluded for
    the same reason.
    """
    results = source_dir / "results.jsonl"
    if not results.exists():
        return {}
    samples: dict[str, list[dict]] = {}
    for line in results.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("match_type") == "judge_error" or r.get("status") == "infra_error":
            continue
        tokens = r.get("tokens") or {}
        samples.setdefault(r["task_id"], []).append({
            "score": r["score"],
            "tokens": int(tokens.get("input", 0)) + int(tokens.get("output", 0)),
            "rollout": str(source_dir / f"{r['task_id']}__r{r['rep']}"),
        })
    return samples


def make_entry(member: str, workflow: str, samples: list[dict]) -> dict:
    reps = len(samples)
    passes = sum(1 for s in samples if s["score"] > 0)
    pass_rate = passes / reps
    if reps >= CONFIRM_REPS and pass_rate >= CONFIRM_RATE:
        status = "confirmed"
    elif passes:
        status = "provisional"
    else:
        status = "fail"
    return {
        "member": member,
        "workflow": workflow,
        "reps": reps,
        "passes": passes,
        "pass_rate": round(pass_rate, 4),
        # Mean over ALL reps (not just passing ones): the expected spend of
        # sending this member at this task, failures included.
        "mean_tokens": round(sum(s["tokens"] for s in samples) / reps),
        "status": status,
        "rollouts_pass": [s["rollout"] for s in samples if s["score"] > 0],
    }


def beats(challenger: dict, owner: dict, token_margin: float) -> tuple[bool, str]:
    """Lexicographic challenge: strength (status, pass_rate) desc, then tokens asc.

    A same-strength challenger must undercut the owner's mean tokens by
    `token_margin`, or the challenge is rejected — without the margin two
    equivalent members swap a task back and forth on run-to-run token noise.
    """
    c_rank, o_rank = STATUS_RANK[challenger["status"]], STATUS_RANK[owner["status"]]
    if c_rank != o_rank:
        return c_rank > o_rank, "stronger" if c_rank > o_rank else ""
    if challenger["pass_rate"] != owner["pass_rate"]:
        return challenger["pass_rate"] > owner["pass_rate"], "stronger"
    if challenger["mean_tokens"] < owner["mean_tokens"] * (1 - token_margin):
        return True, "cheaper"
    return False, ""


def discover_members(opt_root: Path) -> list[dict]:
    """Evaluated members in canonical fold order: seed first, then iterations.

    Each member's evidence is its eval dir plus, if present, its targeted
    confirmation dir under confirm/<member>/.
    """
    members = []
    baseline = opt_root / "evidence/baseline"
    if (baseline / "report.json").exists():
        report = json.loads((baseline / "report.json").read_text())
        members.append({"name": "seed", "workflow": report["workflow"], "sources": [baseline]})
    for iter_dir in sorted(opt_root.glob("iter_*")):
        eval_dir = iter_dir / "eval"
        if not (eval_dir / "report.json").exists():
            continue
        report = json.loads((eval_dir / "report.json").read_text())
        members.append({"name": iter_dir.name, "workflow": report["workflow"], "sources": [eval_dir]})
    for m in members:
        # Flat confirm/<member> is the legacy first round; later rounds live in
        # confirm/round_NNN/<member>. Every round is a separate runner --out dir:
        # runner REWRITES results.jsonl at --out with only the current jobs, so
        # reusing a dir destroys the earlier round's graded rows (it happened —
        # round 2 into the flat dirs cost 6 members their round-1 evidence), and
        # resuming old rollouts silently re-grades them instead of sampling the
        # fresh attempts a new confirmation round is for.
        candidates = [opt_root / "confirm" / m["name"],
                      *sorted(opt_root.glob(f"confirm/round_*/{m['name']}"))]
        m["sources"] += [c for c in candidates if (c / "results.jsonl").exists()]
    return members


def next_confirm_round(opt_root: Path) -> Path:
    """The out-dir root for the next confirmation round (flat confirm/ = round 1)."""
    taken = {int(p.name.split("_")[1]) for p in opt_root.glob("confirm/round_*")
             if p.name.split("_")[1].isdigit()}
    return opt_root / "confirm" / f"round_{max(taken, default=1) + 1:03d}"


def build_book(opt_root: Path, token_margin: float = TOKEN_MARGIN) -> dict:
    opt_root = opt_root.resolve()
    state = json.loads((opt_root / "state.json").read_text()) if (opt_root / "state.json").exists() else {}
    members = discover_members(opt_root)

    tasks: dict[str, dict] = {}
    member_meta = {}
    for m in members:
        merged: dict[str, list[dict]] = {}
        for src in m["sources"]:
            for tid, samples in collect_samples(src).items():
                merged.setdefault(tid, []).extend(samples)
        member_meta[m["name"]] = {
            "workflow": m["workflow"],
            "sources": [str(s) for s in m["sources"]],
            "n_tasks": len(merged),
        }
        for tid, samples in merged.items():
            entry = make_entry(m["name"], m["workflow"], samples)
            task = tasks.setdefault(tid, {"owner": None, "entries": {}, "log": []})
            task["entries"][m["name"]] = entry
            if entry["status"] == "fail":
                continue
            if task["owner"] is None:
                task["owner"] = m["name"]
                task["log"].append(f"{m['name']} claimed (first solve, {entry['mean_tokens']} tok)")
            else:
                owner_entry = task["entries"][task["owner"]]
                won, why = beats(entry, owner_entry, token_margin)
                if won:
                    task["log"].append(
                        f"{m['name']} took over from {task['owner']} ({why}: "
                        f"{entry['status']} {entry['passes']}/{entry['reps']} @ {entry['mean_tokens']} tok "
                        f"vs {owner_entry['status']} {owner_entry['passes']}/{owner_entry['reps']} @ {owner_entry['mean_tokens']} tok)"
                    )
                    task["owner"] = m["name"]
                else:
                    task["log"].append(f"{m['name']} challenged, rejected")

    universe = sorted(tasks)
    union = [t for t in universe if tasks[t]["owner"]]
    confirmed = [t for t in universe if any(e["status"] == "confirmed" for e in tasks[t]["entries"].values())]
    provisional_only = sorted(set(union) - set(confirmed))
    unsolved = sorted(set(universe) - set(union))

    # Greedy minimum cover over union-solved sets: the smallest roster that
    # keeps every solved task solved — the pre-image of the module library.
    solved_by = {m["name"]: {t for t in universe if tasks[t]["entries"].get(m["name"], {}).get("passes", 0) > 0}
                 for m in members}
    remaining, cover_set = set(union), []
    while remaining:
        name, solved = max(solved_by.items(), key=lambda kv: len(kv[1] & remaining))
        gain = len(solved & remaining)
        if not gain:
            break
        cover_set.append({"member": name, "marginal": gain})
        remaining -= solved

    return {
        "domain": state.get("domain"),
        "opt_root": str(opt_root),
        "params": {"token_margin": token_margin, "confirm_reps": CONFIRM_REPS,
                   "confirm_rate": round(CONFIRM_RATE, 4)},
        "member_order": [m["name"] for m in members],
        "members": member_meta,
        "tasks": tasks,
        "coverage": {
            "universe": len(universe),
            "union": len(union),
            "confirmed": len(confirmed),
            "unsolved": unsolved,
            "provisional_only": provisional_only,
        },
        "cover_set": cover_set,
    }


def diff_books(old: dict | None, new: dict) -> dict:
    """What one member's arrival (or a confirmation round) changed. Feeds the
    round record in optimize.py: book movement is progress even when the
    aggregate frontier does not move."""
    old_tasks = (old or {}).get("tasks", {})
    old_owner = {t: rec["owner"] for t, rec in old_tasks.items()}
    old_union = {t for t, o in old_owner.items() if o}
    claimed, took_over = [], []
    for tid, rec in new["tasks"].items():
        was, now = old_owner.get(tid), rec["owner"]
        if now and not was:
            claimed.append(tid)
        elif now and was and now != was:
            took_over.append({"task": tid, "from": was, "to": now, "why": rec["log"][-1]})
    return {
        "claimed": sorted(claimed),
        "took_over": sorted(took_over, key=lambda d: d["task"]),
        "union": f"{len(old_union)} -> {new['coverage']['union']}",
        "confirmed": new["coverage"]["confirmed"],
    }


def confirm_targets(book: dict) -> dict[str, list[str]]:
    """provisional-only coverage grouped by current owner: the cheapest set of
    targeted re-runs that settles whether the union number is real."""
    targets: dict[str, list[str]] = {}
    for tid in book["coverage"]["provisional_only"]:
        targets.setdefault(book["tasks"][tid]["owner"], []).append(tid)
    return {m: sorted(tids) for m, tids in sorted(targets.items())}


def emit_confirm_commands(book: dict, repeats: int, max_tokens: int, max_sec: int) -> list[str]:
    opt_root = Path(book["opt_root"])
    round_root = next_confirm_round(opt_root)
    cmds = []
    for member, tids in confirm_targets(book).items():
        workflow = book["members"][member]["workflow"]
        out = round_root / member
        cmds.append(
            f"PYTHONPATH=bench python3 bench/owf_bench/core/runner.py "
            f"--domain {book['domain']} --workflow {workflow} --subset train "
            f"--task-ids {','.join(tids)} --repeats {repeats} "
            f"--out {out} --max-tokens {max_tokens} --max-wallclock-sec {max_sec} "
            f"--workers {min(len(tids) * repeats, 8)}"
        )
    return cmds


def summarize(book: dict) -> str:
    cov = book["coverage"]
    lines = [
        f"{book['domain']}  members: {len(book['member_order'])}  universe: {cov['universe']}",
        f"  union coverage (solved at least once): {cov['union']}/{cov['universe']}",
        f"  confirmed coverage (>= {book['params']['confirm_reps']} reps, "
        f">= {book['params']['confirm_rate']:.0%} pass): {cov['confirmed']}/{cov['universe']}",
        f"  provisional-only ({len(cov['provisional_only'])}): {', '.join(cov['provisional_only'])}",
        f"  unsolved by anyone ({len(cov['unsolved'])}): {', '.join(cov['unsolved'])}",
        "  greedy cover set: " + " + ".join(f"{c['member']}(+{c['marginal']})" for c in book["cover_set"]),
    ]
    owners: dict[str, int] = {}
    for rec in book["tasks"].values():
        if rec["owner"]:
            owners[rec["owner"]] = owners.get(rec["owner"], 0) + 1
    lines.append("  tasks owned: " + ", ".join(f"{m}:{n}" for m, n in
                 sorted(owners.items(), key=lambda kv: -kv[1])))
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--opt-root", required=True)
    p.add_argument("--apply", action="store_true", help="write <opt-root>/task_book.json (default: dry run)")
    p.add_argument("--emit-confirm", action="store_true",
                   help="print targeted runner commands that confirm provisional-only coverage")
    p.add_argument("--token-margin", type=float, default=TOKEN_MARGIN)
    p.add_argument("--confirm-repeats", type=int, default=2)
    # Budgets for emitted commands — MUST match the budget the run was measured
    # under (bcplus: 600000, realmath: 300000), same rule as optimize.py.
    p.add_argument("--max-tokens", type=int, default=600_000)
    p.add_argument("--max-sec", type=int, default=1800)
    args = p.parse_args()

    opt_root = Path(args.opt_root).resolve()
    book_path = opt_root / "task_book.json"
    old = json.loads(book_path.read_text()) if book_path.exists() else None
    book = build_book(opt_root, args.token_margin)

    print(summarize(book))
    delta = diff_books(old, book)
    if old:
        print(f"  delta vs stored book: claimed {delta['claimed'] or 'none'}, "
              f"took_over {[d['task'] for d in delta['took_over']] or 'none'}, union {delta['union']}")

    if args.emit_confirm:
        cmds = emit_confirm_commands(book, args.confirm_repeats, args.max_tokens, args.max_sec)
        print("\nconfirmation runs (from repo root; rollouts land in confirm/<member>/, "
              "historical eval dirs stay untouched):")
        for c in cmds:
            print(f"  {c}")

    if args.apply:
        book_path.write_text(json.dumps(book, indent=1, ensure_ascii=False))
        print(f"\nwrote {book_path}")
    else:
        print("\ndry run — pass --apply to write task_book.json")


if __name__ == "__main__":
    main()
