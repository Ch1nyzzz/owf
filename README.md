# owf — Orchestration Workflow Optimization

`owf` studies how to improve agent orchestration without evolving the benchmark
harness itself. The harness is kept thin and stable (sandbox, state, agent loop,
grading, and accounting); the search target is the workflow that composes full
tool-using agent rollouts, glue JavaScript, and in-loop hooks.

Candidates are measured on two axes: task score (higher is better) and total
input+output tokens (lower is better). The optimizer keeps the exact Pareto
frontier; measured score variance is evidence for later confirmation, not a
tolerance that may discard a higher-scoring candidate.

## Repository layout

- `docs/DSL.md` — frozen runtime contract implemented by the executor.
- `docs/CONTRACTS.md` — component/assembly declarations used by contract-native
  search. This is a knowledge layer above the DSL, not an executor extension.
- `executor/` — Node/TypeScript harness that runs a workflow or meta-harness
  agent and writes an auditable `journal.jsonl`. It uses the pinned official
  `@mariozechner/pi-agent-core` release.
- `workflows/` — seed and candidate workflows; this is the main optimization
  surface.
- `bench/owf_bench/core/` — evaluation runner, optimizer, task book, component
  assembler, meta delegation, and distillation.
- `bench/owf_bench/metaharness_arm/` — free-JavaScript meta-harness comparison
  arm.
- `bench/owf_bench/gepa_arm/` — GEPA prompt-only comparison arm.
- `scripts/` — canonical detached launchers and held-out orchestration.
- `configs/models.yaml` — model registry. Credentials come from environment
  variables and must never be committed.
- `data/` — datasets and split indexes (normally gitignored except committed
  split files).
- `runs/` — run state, reports, journals, candidates, and lab evidence.

## Current experiment protocol

The unit of comparison is a candidate measured on the same dataset split,
model substrate, thinking setting, rollout budget, and concurrency policy as
its baseline. A substrate or judging change invalidates historical baseline
numbers; re-measure the seed before comparing new candidates.

The current end-to-end flow is:

1. **Freeze the split and substrate.** Keep test tasks sealed during search.
2. **Measure the seed baseline.** Produce `report.json` and `stability.json` on
   the same substrate and budget that candidates will use.
3. **Search on train.** Evaluate candidates and update both evidence ledgers:
   the aggregate `(score, tokens)` Pareto frontier and the per-task champion
   book.
4. **Confirm claims.** Re-run newly claimed or more cheaply solved tasks in a
   separate confirmation directory. Probe outputs are exploratory evidence and
   never enter the task book.
5. **Select a champion.** Prefer the best confirmed score, using tokens to
   choose the leaner candidate among score ties.
6. **Run held-out evaluation.** Compare contemporaneous arms on identical test
   task IDs. Fresh, disjoint samples may be used for a rematch; test results do
   not feed back into search.

### Evidence ledgers

The optimizer tracks two complementary notions of progress:

- **Pareto frontier:** aggregate score up, token use down. Exact measurements
  determine frontier admission.
- **Task book:** records which lab member solves each training task and at what
  mean token cost. A candidate can make useful progress by solving residual
  tasks or taking over known tasks more cheaply even if it does not enter the
  aggregate frontier.

In contract-native runs, `lab/components.json` adds a third view: the declared
component inventory and membership matrix. Each candidate summary must include
a valid manifest declaring a `NEW_COMPONENT`, `NEW_ASSEMBLY`, or `NEW_SLOT`
action. A missing or invalid manifest voids that round. See
[`docs/CONTRACTS.md`](docs/CONTRACTS.md) for the declaration protocol.

The optimizer may spend at most eight sanctioned probe rollouts per round on
selected training IDs. Probes use the real per-rollout budget, are saved below
the iteration's `probe/` directory, and are excluded from scored ledgers.

## Experiment arms

The repository supports three comparison arms:

| arm | search surface | launcher |
|---|---|---|
| workflow | workflow DSL plus hooks; optionally contract-native manifests | `scripts/launch_opt.sh` |
| meta-harness | paper-faithful free-JavaScript agent proposer | `scripts/launch_metaharness.sh` |
| GEPA | prompt-only optimization baseline | `scripts/launch_gepa.sh` |

All arms in a comparison must start from the same measured iteration-0 baseline
and use matching evaluation budgets. `scripts/launch_bcplus_dual_arm.sh` is the
BCPlus substrate-specific example that re-measures the seed when necessary and
launches the workflow and meta-harness arms together.

## Running experiments

Set the model and judge credentials expected by `configs/models.yaml` in a local
`.env`, then build/install the project dependencies used by the executor and
Python bench environment.

Start the standard workflow arm with:

```bash
scripts/launch_opt.sh bcplus runs/opt_bcplus_experiment_name --iters 10 --contract-native
scripts/launch_opt.sh realmath runs/opt_realmath_experiment_name --iters 10 --contract-native
```

Start comparison arms with:

```bash
scripts/launch_metaharness.sh bcplus runs/metaharness_bcplus_experiment_name --iterations 10
scripts/launch_gepa.sh bcplus runs/gepa_bcplus_experiment_name --max-metric-calls 600
```

The launchers use `setsid` and `nohup`, reject duplicate drivers on the same run
root, and resume the workflow optimizer from completed state. Do not start a
long optimization as a plain interactive-shell background job: it can die with
the session and lose an unfinished round.

Useful direct evaluation forms are:

```bash
PYTHONPATH=bench python3 bench/owf_bench/core/runner.py \
  --domain bcplus --workflow workflows/bcplus/seed_parity.js \
  --subset train --repeats 1 --workers 64 --out runs/example_seed \
  --max-tokens 600000 --max-wallclock-sec 1800

PYTHONPATH=bench python3 bench/owf_bench/core/runner.py \
  --domain bcplus --agent-file path/to/agent.mjs \
  --subset test --task-ids id1,id2,id3 --workers 64 \
  --out runs/example_meta_test --max-tokens 600000 \
  --max-wallclock-sec 1800
```

Treat the commands above as patterns, not permission to reuse stale run names or
baseline measurements. Pin the exact task IDs, budgets, model endpoint, thinking
mode, and judge configuration in the experiment's run script.

## Run outputs

A workflow optimization root normally contains:

```text
runs/<experiment>/
├── state.json              # completed rounds and Pareto frontier
├── task_book.json          # per-task ownership and confirmed evidence
├── NOTES.md                # persistent optimizer notes
├── driver.log
├── iter_NNN/
│   ├── candidate.js
│   ├── summary.json        # rationale and, for v9, component manifest
│   ├── eval/               # graded training evaluation
│   └── probe/              # exploratory, never scored into ledgers
├── confirm/
│   └── round_NNN/          # independent confirmation rounds
└── lab/
    ├── components.json     # component inventory/membership
    ├── attribution.json    # component-level evidence when available
    └── playbook*.md        # distilled dispatch policy when available
```

Meta-harness and GEPA arms have their own frontier/state formats; use their
launcher and driver logs rather than assuming the workflow-arm layout.

## Benchmarks

| domain | benchmark | grading |
|---|---|---|
| terminal | Terminal-Bench 2 (Harbor tasks; split copied with provenance) | verifier reward |
| math | RealMath-133 (SymPy-verifiable subset) | symbolic check |
| deep research | BrowseComp-Plus (fixed corpus) | judge against gold answer |
| finance | FinSearchComp T2+T3 static-answer subsets | rubric-band binary judge |

Current scripts and checked-in experimental evidence focus primarily on
RealMath and BrowseComp-Plus (`bcplus`). The table records the broader project
targets; it does not imply that every launcher supports every domain.

## Reproducibility and independence

- Never compare candidates measured on different substrates or budgets.
- Never let probe, train, or task-book evidence reveal held-out answers.
- Keep confirmation outputs separate; reusing an output directory destroys the
  provenance of the evidence.
- Treat `judge_error` and `infra_error` as infrastructure outcomes, not task-book
  evidence.
- Keep total API concurrency within the limit recorded by the experiment.
- Do not commit credentials or local `.env` files.

This project remains standalone: there are no imports or symlinks into
WorldCalib or local pi forks. External assets are copied with provenance or
declared explicitly (for example through a lab import manifest), and the pi
dependency is the pinned official upstream release.
