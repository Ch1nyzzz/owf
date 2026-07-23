# owf — Orchestration Workflow Optimization

Research project: instead of evolving a benchmark harness, we freeze a **thin, robust harness**
(sandbox + state + agent loop) and evolve a per-domain **workflow.js** — an orchestration program
that composes agent nodes (each a full tool-using rollout with its own system prompt, model, and
tools), glue JS, and in-loop rails (hooks). Candidates are evaluated on a two-axis
(score, cost) Pareto frontier — cost is CNY per task, with output tokens billed at
twice the input rate, so "same score for less money" is a win in its own right.

## Layout

- `docs/DSL.md` — the frozen workflow contract (v1). The executor implements exactly this, nothing more.
- `executor/` — Node/TypeScript thin harness. Runs one workflow.js against one task, writes `journal.jsonl`.
  Built on official upstream [`@mariozechner/pi-agent-core`](https://www.npmjs.com/package/@mariozechner/pi-agent-core) (pinned).
- `workflows/` — seed and candidate workflows per domain (the optimization target; NOT harness code).
- `bench/` — Python evaluation driver: task loading, split management, k-repeats, grading, (score, tokens) accounting.
- `scripts/launch_opt.sh` — how to start an optimization run. Detaches the driver with `setsid` and pins
  the per-domain eval budget; never start `optimize.py` as a plain shell background job (it dies with the session).
- `configs/models.yaml` — SUT model registry (OpenAI-compatible endpoints; keys via env, never committed).
- `data/` — datasets and indexes (gitignored except committed split files).

## Benchmarks (Phase 0 targets)

| domain | benchmark | grading |
|---|---|---|
| terminal | Terminal-Bench 2 (harbor tasks, split copied from WorldCalib `data/tb2/split_v2.json`) | verifier reward |
| math | RealMath-133 (ethz-spylab/RealMath, SymPy-verifiable subset) | symbolic check |
| deep research | BrowseComp-Plus (fixed 100K-doc corpus) | judge vs gold string |
| finance | FinSearchComp T2+T3 (static-answer subsets) | rubric-band binary judge |

## Independence rules

This project is standalone by design: no imports/symlinks into WorldCalib or local pi forks.
Assets are copied (with provenance noted) or written fresh. The pi dependency is the official
upstream npm release, pinned exactly.
