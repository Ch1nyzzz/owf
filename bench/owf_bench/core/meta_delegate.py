"""Meta-agent delegation driver: playbook + task -> assembly spec -> rollout.

For each task the meta model reads ONLY the generated playbook (never the
ledgers), and returns either {"preset": name} or {"assembly": spec}. The spec
is validated by the assembler's contract check; an invalid or unparseable
response gets one retry with the error appended, then falls back to the
default preset — the system can never do worse than its fallback chain by
construction (modulo the meta call's own token cost, which is accounted
separately from the workflow's).

Per task: render (or copy) the workflow, run the standard runner in an
isolated out dir (one dir per task — runner rewrites results.jsonl per
invocation), then aggregate a delegation report comparing against the task
book's owner, the champion preset, and the seed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path

from owf_bench.core.assemble import render, validate_spec
from owf_bench.core.roster import FILE_PRESETS, PRESETS

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PRESET = "iter_001"

SPEC_HELP = """Assembly spec fields (JSON):
  prompt: "evidence_lead" | "seed_persistent" | "bounded_researcher"
  prompt_variant: "final_line" | "one_line_only" (evidence_lead only)
  decoding: "greedy_nothink" | null
  turn_budget: int in [8, 64]
  cutoff_turn: int below turn_budget, or null
  closure: null | {"type": "post_editor", "window": 8000, "maxTurns": 3}
          | {"type": "inhook_editor", "window": 16000, "maxTurns": 2}  (requires cutoff_turn)
  output: "regex_extractor" | "schema_direct" | "raw"
    (schema_direct: no closure, no verifier, not with seed_persistent)
  verifier: "exact_name" | null   (requires output=regex_extractor)
  monitor: "starvation_close" | null (requires cutoff_turn)"""


def call_meta(prompt: str, cfg: dict) -> tuple[str, dict]:
    payload = {"model": cfg["model"], "messages": [{"role": "user", "content": prompt}],
               "temperature": 0.2, "max_tokens": 2048}
    req = urllib.request.Request(
        f"{cfg['base_url'].rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {cfg['key']}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        body = json.load(resp)
    usage = body.get("usage") or {}
    return body["choices"][0]["message"]["content"], usage


def parse_decision(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError("no JSON object in meta response")
    decision = json.loads(m.group(0))
    if "preset" in decision:
        name = decision["preset"]
        if name not in PRESETS and name not in FILE_PRESETS:
            raise ValueError(f"unknown preset: {name}")
        return decision
    if "assembly" in decision:
        errors = validate_spec(decision["assembly"])
        if errors:
            raise ValueError("; ".join(errors))
        return decision
    raise ValueError("decision must contain 'preset' or 'assembly'")


def decide(task: dict, playbook: str, cfg: dict) -> tuple[dict, dict]:
    base = (
        f"{playbook}\n\n---\n{SPEC_HELP}\n\n---\n"
        f"Question to dispatch:\n{task['instruction']}\n\n"
        "Decide the workflow for THIS question. Reply with ONLY a JSON object: "
        '{"preset": "<name>", "reason": "..."} or {"assembly": {<spec>}, "reason": "..."}.'
    )
    meta_stats = {"tokens": 0, "retries": 0, "fallback": False}
    prompt = base
    for attempt in range(2):
        try:
            text, usage = call_meta(prompt, cfg)
            meta_stats["tokens"] += int(usage.get("total_tokens") or 0)
            decision = parse_decision(text)
            meta_stats["retries"] = attempt
            return decision, meta_stats
        except Exception as exc:  # noqa: BLE001 — any failure funnels into retry/fallback
            err = str(exc)[:400]
            prompt = base + f"\n\nYour previous reply was invalid ({err}). Reply with ONLY the JSON object."
    meta_stats["fallback"] = True
    return {"preset": DEFAULT_PRESET, "reason": f"fallback after invalid responses"}, meta_stats


def materialize(decision: dict, opt_root: Path, dest: Path, name: str) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if "preset" in decision:
        preset = decision["preset"]
        if preset in FILE_PRESETS:
            shutil.copy(opt_root / FILE_PRESETS[preset], dest)
        else:
            dest.write_text(render(PRESETS[preset], name))
        return f"preset:{preset}"
    dest.write_text(render(decision["assembly"], name))
    return "novel"


def run_task(task_id: str, workflow: Path, domain: str, out_dir: Path, repeats: int,
             max_tokens: int, max_sec: int) -> dict | None:
    cmd = ["python3", str(ROOT / "bench/owf_bench/core/runner.py"), "--domain", domain,
           "--workflow", str(workflow), "--subset", "train", "--task-ids", task_id,
           "--repeats", str(repeats), "--workers", str(repeats), "--out", str(out_dir),
           "--max-tokens", str(max_tokens), "--max-wallclock-sec", str(max_sec)]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=max_sec * repeats + 600,
                          env={**os.environ, "PYTHONPATH": str(ROOT / "bench")})
    report = out_dir / "report.json"
    if not report.exists():
        print(f"  {task_id}: eval failed: {proc.stderr[-200:]}")
        return None
    return json.loads(report.read_text())


def reference_scores(book: dict, task_id: str) -> dict:
    rec = book["tasks"].get(task_id, {})
    entries = rec.get("entries", {})
    pick = lambda m: entries[m]["pass_rate"] if m in entries else None
    return {"owner": rec.get("owner"), "owner_rate": pick(rec.get("owner")),
            "champion_rate": pick("iter_007"), "seed_rate": pick("seed")}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--opt-root", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--task-ids", help="comma-separated; default: all training tasks in the book")
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--max-tokens", type=int, default=600_000)
    p.add_argument("--max-sec", type=int, default=1800)
    p.add_argument("--meta-base-url", default=os.environ.get("META_BASE_URL", "https://api.gpugeek.com/v1"))
    p.add_argument("--meta-model", default=os.environ.get("META_MODEL", "Vendor3/DeepSeek-V4-Flash"))
    p.add_argument("--meta-key-env", default=os.environ.get("META_KEY_ENV", "SOLVER_API_KEY"))
    args = p.parse_args()

    opt_root = Path(args.opt_root).resolve()
    out_root = Path(args.out).resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    book = json.loads((opt_root / "task_book.json").read_text())
    playbook = (opt_root / "lab/playbook.md").read_text()
    domain = book["domain"]
    cfg = {"base_url": args.meta_base_url, "model": args.meta_model,
           "key": os.environ.get(args.meta_key_env, "")}

    from owf_bench.core.runner import load_dotenv, load_tasks
    load_dotenv()
    cfg["key"] = cfg["key"] or os.environ.get(args.meta_key_env, "")
    wanted = [t.strip() for t in args.task_ids.split(",")] if args.task_ids else sorted(book["tasks"])
    tasks = {t["id"]: t for t in load_tasks(domain, "train", None, wanted)}

    records = []
    for tid in wanted:
        decision, meta_stats = decide(tasks[tid], playbook, cfg)
        wf = out_root / "assembled" / f"{tid}.js"
        kind = materialize(decision, opt_root, wf, f"meta-{tid}")
        report = run_task(tid, wf, domain, out_root / "rollouts" / tid, args.repeats,
                          args.max_tokens, args.max_sec)
        score = report["task_scores"].get(tid) if report else None
        rec = {"task": tid, "choice": kind, "reason": str(decision.get("reason", ""))[:300],
               "meta": meta_stats, "score": score,
               "tokens": report["task_tokens"].get(tid) if report and "task_tokens" in report else None,
               "refs": reference_scores(book, tid)}
        if "assembly" in decision:
            rec["assembly"] = decision["assembly"]
        records.append(rec)
        print(f"{tid}: {kind} -> score {score} (owner {rec['refs']['owner']} "
              f"{rec['refs']['owner_rate']}, champion {rec['refs']['champion_rate']}) {rec['reason'][:80]}")
        (out_root / "delegation.json").write_text(json.dumps(records, indent=1, ensure_ascii=False))

    scored = [r for r in records if r["score"] is not None]
    summary = {
        "n": len(records), "evaluated": len(scored),
        "meta_accuracy": round(sum(r["score"] for r in scored) / len(scored), 4) if scored else None,
        "owner_reference": round(sum(r["refs"]["owner_rate"] or 0 for r in scored) / len(scored), 4) if scored else None,
        "champion_reference": round(sum(r["refs"]["champion_rate"] or 0 for r in scored) / len(scored), 4) if scored else None,
        "seed_reference": round(sum(r["refs"]["seed_rate"] or 0 for r in scored) / len(scored), 4) if scored else None,
        "choices": {k: sum(1 for r in records if r["choice"] == k) for k in {r["choice"] for r in records}},
        "fallbacks": sum(1 for r in records if r["meta"]["fallback"]),
        "meta_tokens_total": sum(r["meta"]["tokens"] for r in records),
        "meta_model": cfg["model"],
    }
    (out_root / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False))
    print(json.dumps(summary, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
