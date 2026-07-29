# Component contracts v1 — contract-first search

Status: draft, calibrated against the post-hoc decomposition of opt_bcplus_v8
(11 members → 13 components, see `runs/opt_bcplus_v8/lab/components.json`).
The DSL (`docs/DSL.md`) is the runtime substrate and does not change; contracts
are the knowledge layer on top: everything that lands in the library declares
what it is, where it plugs in, and what it needs — so the ledgers (task book,
membership matrix, attribution) grow natively instead of by archaeology.

## Slot vocabulary v1

Derived from the bcplus decomposition; NEW_SLOT (below) is how it grows.

| slot | holds | bcplus examples |
|---|---|---|
| `research_prompt` | system prompt of the investigator node | seed_persistent, evidence_lead, bounded_researcher |
| `decoding` | temperature / thinking settings | greedy_nothink |
| `turn_budget` | maxTurns of the investigator | 56–64 |
| `cutoff_rail` | hard research deadline (onTurn inject + preToolUse block) | rail.cutoff@56 |
| `closure` | converts a deadline-hitting run into an answer | post_editor, inhook_editor |
| `output_contract` | how the answer leaves the workflow | schema_direct, regex_extractor |
| `verifier` | optional post-hoc answer checks | exact_name |
| `monitor_rail` | mid-run trajectory monitors | starvation_close |
| `topology` | node structure | single_lead, scout_resolver |

## Component declaration

A component is a module in `components/<domain>/<id>.js` exporting:

```js
export const contract = {
  id: 'closure.inhook_editor',
  slot: 'closure',
  requires: ['cutoff_rail'],          // slots that must be filled for this to function
  provides: ['bounded_final_answer'], // capability tags, free vocabulary, curated in review
  params: {                           // JSON-schema of tunable settings
    window:  { type: 'integer', default: 16000 },
    maxTurns:{ type: 'integer', default: 2 },
  },
}
export function apply(ctx, assembly, params) { /* returns hook fns / node fn / options patch */ }
```

Rules:
- **Anonymous landing is forbidden.** Any code a search round ships must arrive
  as a component with a contract (or a param change to one). Freedom of FORM is
  untouched — the implementation body is arbitrary DSL code.
- Params carry defaults; an assembly overrides them explicitly. Param variants
  are the same component (the attribution ledger contrasts them as param pairs).

## Assembly declaration

An assembly is a config, not code: `assemblies/<domain>/<name>.json`

```json
{ "research_prompt": "prompt.evidence_lead",
  "decoding": null,
  "turn_budget": 60,
  "cutoff_rail": { "id": "rail.cutoff", "params": { "turn": 56 } },
  "closure":    { "id": "closure.inhook_editor", "params": { "window": 16000, "maxTurns": 2 } },
  "output_contract": "out.regex_extractor",
  "verifier": null, "monitor_rail": null, "topology": "single_lead" }
```

The executor's assembler instantiates config → workflow, then runs BOTH gates:
contract check (slot exists, `requires` satisfied, params validate) and the
existing `--validate-only`. The 11 bcplus members become 11 retro assemblies
and must reproduce their task-book records as the library's regression suite.

## Search actions (per round, declared in summary.json)

| action | meaning | ledger effect |
|---|---|---|
| `NEW_COMPONENT` | new component (or variant) into an existing slot | matrix gains a column |
| `NEW_ASSEMBLY` | recombination / param change of existing components | matrix gains a row; de-confounds |
| `NEW_SLOT` | skeleton change: declare a new slot + its first component | vocabulary grows; must ship contract |

`NEW_SLOT` is the escape hatch that keeps search wild enough to discover
topologies the current factorization cannot express (the closure editor itself
was such a discovery). Worst case it lands one oversized component whose later
decomposition debt is bounded to itself.

## Evidence the driver serves back

Per round, alongside the frontier and lab-coverage blocks: the component
utility table (attribution summary), the untested-combination list (bundles the
matrix cannot yet separate — an explicit invitation for de-confounding
NEW_ASSEMBLY rounds), and per-slot option shelves.

## Migration

- Legacy runs (bcplus/realmath v8): post-hoc decomposition, kept as
  `runs/<run>/lab/components.json`; they bootstrap the slot vocabulary.
- Next domain runs contract-native from round 1; the proposer's representation
  contract swaps free `candidate.js` for the three actions above.
