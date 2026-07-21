# owf Workflow DSL — Contract v1 (FROZEN)

This document is the **complete** interface between a candidate `workflow.js` and the executor.
The executor implements exactly this surface. After M3 (TB2 parity), any change to this contract
is a breaking event: it invalidates cross-iteration comparability and requires re-running parity.
Additions require a version bump and an explicit decision.

## 1. Module shape

A workflow is a single ES module. It must export:

```js
export const meta = { name: 'tb2-seed', version: 1 }   // pure literal, no computed values
export default async function run(ctx) { ... return result }
```

- `run(ctx)` is called once per task. Its return value is the workflow's **final answer**
  (string or JSON-serializable object; domain runner defines how it is graded).
- `ctx` is the only capability the workflow receives (see §2). There is no ambient authority:
  no filesystem, no network, no process, no timers.

## 2. `ctx` — injected capabilities

```ts
ctx = {
  task,        // frozen task payload: { id, instruction, ...domain fields } — read-only
  agent,       // spawn one agent rollout (§3)
  pipeline,    // per-item staged flow, no barrier (§4)
  parallel,    // barrier concurrency (§4)
  log,         // log(msg: string) → journal narrator line
  budget,      // { totalTokens, spentTokens(), remainingTokens(), elapsedMs() } (§6)
}
```

## 3. `agent(prompt, opts)` — one rollout node

```ts
const out = await ctx.agent(prompt, {
  system: string,            // REQUIRED. Verbatim system prompt. Executor adds nothing.
  model: string,             // REQUIRED. Key into configs/models.yaml (e.g. 'deepseek-chat', 'minimax-m3').
  tools: string[],           // tool names from the harness registry for this domain (§7). Default [].
  maxTurns: number,          // default 30, hard cap 200.
  temperature: number,       // default 0.0.
  thinkingLevel: 'off'|'minimal'|'low'|'medium'|'high', // default 'off'; ignored by non-reasoning models.
  schema: object,            // optional JSON-Schema. Forces structured output (§5).
  label: string,             // journal attribution label. Default 'node-<seq>'.
  hooks: { ... },            // in-loop rails (§3.1). Default none.
})
```

Return value:
- no `schema`: the final assistant text (string).
- with `schema`: the validated object.
- **failure → `null`**, never a throw: model/API error after retries, maxTurns exhausted without
  an answer, schema never satisfied, budget exceeded mid-node. The cause is recorded in the
  journal (`node_end.status`). Workflows must handle `null`.

### 3.1 `hooks` — in-loop rails

All hooks are optional async functions. A hook that throws is **ignored for that firing**
(recorded in journal as `hook_error`), the loop continues.

```ts
hooks: {
  preToolUse(call, state)   // before a tool executes.
                            // return { block: string } → tool does not run; the string is the
                            //   error text the model sees. return undefined → allow.
  postToolUse(call, result, state)
                            // after a tool executes, before the model sees the result.
                            // return { inject: string } → an extra user message is appended
                            //   after this tool result. return undefined → nothing.
  onTurn(state)             // after each completed turn (assistant msg + its tool results).
                            // return { stop: true } → end the rollout now (final = last text).
                            // return { inject: string } → steer: user message before next turn.
  onStop(state)             // when the model stops calling tools (would finish).
                            // return { continue: string } → push back: the string is sent as a
                            //   user message and the rollout continues. May fire repeatedly;
                            //   executor hard-caps onStop continuations at 5 per node.
}
```

- `call` = `{ toolName, args }` (args are validated tool arguments, read-only).
- `result` = `{ content: string, isError: boolean, details?: any }` (tool output as the model will see it).
- Hooks MAY call `ctx.agent(...)` (LLM-judged rails). Such nested nodes are journaled like any
  node, count against the budget, and may NOT declare hooks themselves (depth-1 rule).

### 3.2 `state` — read-only rollout view passed to every hook

```ts
state = {
  turn: number,              // 1-based completed-turn counter
  elapsedMs: number,         // wall clock since node start
  tokensSpent: number,       // input+output tokens this node so far
  recentCommands: string[],  // last 20 tool invocations, formatted '<tool>:<primary-arg>'
  transcript: string,        // rendered rollout so far (tail-truncated to 32 KB)
}
```

## 4. Orchestration primitives

```ts
await ctx.pipeline(items, stage1, stage2, ...)
// Each item flows through stages independently — NO barrier between stages.
// stageN receives (prevResult, originalItem, index). A stage that throws or returns null
// drops the item (null in the result array); remaining stages are skipped for it.

await ctx.parallel([thunk1, thunk2, ...])
// Barrier: resolves when all settle. A thunk that throws resolves to null.
// Concurrency cap: 8 concurrent agent nodes per workflow (excess queue).
```

Plain JS (`map/filter/reduce`, `if/for`, string ops, `JSON`, `Math` except `Math.random`) is the glue.

## 5. Structured output (`schema`)

Implemented as a forced tool: the node gets one extra tool `submit_result` whose parameters are
the given schema; the system prompt is suffixed with a fixed one-line instruction to call it when
done. Calling it validates the args and ends the node (`terminate`). Validation failure returns
the validation error as the tool result and the rollout continues (max 3 validation failures →
node fails → `null`).

## 6. Budget & determinism

- Per-task budget: `--max-tokens` (default 400k) and `--max-wallclock-sec` (default 1800) CLI
  flags on the executor. Exceeding either: current node fails (`budget_exceeded`), and any further
  `agent()` call throws — the workflow gets a chance to return a partial answer; if it throws,
  the task fails with a structured cause.
- Banned at sandbox level (throw on call): `Date.now`, `new Date()` (argless), `Math.random`,
  `setTimeout`/`setInterval`, `fetch`, `process`, `require`, dynamic `import`, `eval`,
  `Function` constructor. Elapsed time is available via `ctx.budget.elapsedMs()` / `state.elapsedMs`.
- `meta` must be a pure literal.

## 7. Tool registry (harness-owned)

Workflows can only *select* tools by name; definitions live in the executor. Per-domain sets:

| domain | tools |
|---|---|
| tb2 | `terminal` (exec command in task container) |
| realmath | `python` (sandboxed Python subprocess with sympy; SageMath if available) |
| bcplus | `search` (query fixed local corpus index), `open_doc` (fetch doc by id) |
| finsearch | `web_search` (Tavily/Serper provider chain), `url_fetch` (page → markdown) |

The executor exposes exactly the set for `--domain`; requesting an unknown tool name fails the
node at start (`unknown_tool`).

## 8. Journal (journal.jsonl)

One JSON object per line. Event types:

```
workflow_start {task_id, workflow_name, models_available, domain}
node_start     {node: label, seq, model, tools, maxTurns, has_schema, hook_names}
turn           {node, turn, tokens: {input, output}, toolCalls: [{tool, argsPreview}]}
tool_call      {node, turn, tool, argsPreview, resultPreview, isError, durationMs}
hook_fire      {node, turn, hook, action: 'block'|'inject'|'continue'|'stop'|'none', payloadPreview}
hook_error     {node, turn, hook, error}
node_end       {node, seq, status: 'ok'|'error'|'maxTurns'|'schema_failed'|'budget_exceeded'|'aborted',
                turns, tokens: {input, output}, durationMs, resultPreview}
log            {message}
workflow_end   {status: 'ok'|'workflow_error'|'budget_exceeded'|'timeout',
                result, totalTokens: {input, output}, durationMs, error?}
```

Previews are head-truncated to 2 KB; full node transcripts are written alongside as
`node-<seq>-<label>.jsonl` (the raw pi AgentMessage list). The journal is the **complete evidence
surface** for the optimizer: every hook firing, block reason, and injection is attributable.

## 9. Robustness semantics (executor guarantees)

- Parse/validation failure of workflow.js → exit code 0, `workflow_end.status='workflow_error'`,
  score handled as 0 by the bench runner. The executor process never crashes on candidate code.
- Any uncaught throw inside `run(ctx)` → same.
- The executor exits with non-zero code ONLY for harness-level faults (bad CLI args, missing
  task file, tool infrastructure down) — these are infra errors, not candidate failures, and the
  bench runner retries them.
