export const meta = { name: 'owf-optimizer', version: 4 }

// L0 optimizer: a lead with privileged tools, fed by two axis-split proposers.
// Principles live in the system prompt; noise facts live in the DATA it reads
// (stability report). No case law, no quota rules.
//
// v4 removes the reader layer (v2's `investigate` subagents; v3 was a watchdog rewrite
// local to opt_realmath_v6). Readers existed so the lead could sweep a ~29MB evidence
// surface without filling its 272k window — but the proposer split already provides
// exactly that: each proposer is a disposable context that reads raw evidence and hands
// back one compressed proposal. The proposers ARE the readers. Removing the extra layer
// removes the unbounded fan-out failure class with it (opt_realmath_v6 iter 2 lost its
// round to 38 readers eating the 9000s wall clock before the lead ran) and one hop of
// lossy handoff. The cost: a proposer that reads past its own window dies and its
// proposal arrives as null — paid by the node that overspent, not by the round, which
// continues on the other proposal.

export default async function run(ctx) {
  const t = ctx.task
  const optModel = t.opt_model || 'gpt-5.6-terra'

  // Two proposers, one per axis, run in parallel before the lead writes anything.
  //
  // A single agent told to improve both axes drifts to the cheap one: cutting tokens is
  // predictable and verifiable in a 12-task probe, while raising score is neither. The
  // bcplus run showed exactly that — three consecutive rounds whose own summaries were
  // about token savings ("27.4% fewer tokens", "56-62% below parent cost", "cost
  // containment ... at the same 6/12 score"), and its best score never moved off the seed.
  // Splitting the mandate means the score proposer has no cheaper task to retreat to.
  //
  // Proposers investigate and argue; they do not write workflows and cannot run_probe.
  // Probing is the expensive operation and stays with the lead, spent once on the
  // candidate that actually ships rather than twice on proposals that may not.
  const PROPOSAL_SCHEMA = {
    type: 'object',
    properties: {
      parent: { type: 'string', description: 'which frontier point / earlier candidate you build on, and why' },
      diagnosis: { type: 'string', description: 'the failure or waste pattern, with task ids and journal paths' },
      mechanism: { type: 'string', description: 'the concrete change: which node, which prompt, which rail, which turn cap' },
      code_sketch: { type: 'string', description: 'the workflow.js fragment that implements it — enough for the lead to integrate verbatim' },
      expected: { type: 'string', description: 'predicted score and token effect, and which tasks should move' },
      risk: { type: 'string', description: 'what this could break, and what evidence would disconfirm it' },
    },
    required: ['parent', 'diagnosis', 'mechanism', 'code_sketch', 'expected', 'risk'],
  }

  const proposerCommon = [
    'You are one of two proposers serving the optimizer of an agent workflow. You do not write the candidate:',
    'you investigate the evidence and hand the lead ONE concrete, implementable proposal for your assigned axis.',
    'Ground every claim in evidence you actually read — cite task ids and journal paths. A proposal the lead cannot',
    'trace back to data is worthless to it.',
    'You read the evidence yourself (read_file, list_dir), and your context is the reading budget: a full-size read',
    'is 48KB and nothing behind you catches an overflow. Sample deliberately — a handful of representative rollouts',
    'read closely beats skimming everything. Once the evidence supports one mechanism, stop reading and write.',
    'Your code_sketch must fit the DSL (evidence/DSL.md) and use only deepseek-v4-flash.',
    'Propose ONE mechanism, the one your evidence supports best — not a menu. Be specific enough to implement.',
  ]

  const propose = (axis, brief) =>
    ctx.agent(`${t.instruction}\n\nYOUR AXIS THIS ROUND:\n${brief}`, {
      system: [...proposerCommon, '', `YOUR AXIS: ${axis}`, brief].join('\n'),
      model: optModel,
      thinkingLevel: 'high',
      tools: ['read_file', 'list_dir'],
      maxTurns: 40,
      schema: PROPOSAL_SCHEMA,
      label: `propose_${axis}`,
    })

  const [scoreProposal, tokenProposal] = await ctx.parallel([
    () =>
      propose(
        'score',
        [
          'RAISE THE SCORE. You own the accuracy axis and nothing else.',
          'Start from the highest-scoring frontier point unless the evidence argues for another parent.',
          'Find tasks that are failing and could be made to pass: read what the rollouts actually answered and why it was',
          'wrong or absent. Check whether a usable result already appeared somewhere in the trajectory and was lost on the',
          'way to the final answer — repairing that path is a different fix from making a node redo the work, and usually',
          'a cheaper one. Then propose the structure, prompt, routing or rail that would fix that failure MODE on unseen',
          'tasks of the same kind.',
          'Extra tokens are allowed when they buy accuracy, but say what they buy: name the mechanism the spend funds.',
          'Do NOT propose a change whose main effect is saving tokens — that is the other proposer\'s job, and a round',
          'where both of you optimise cost is a wasted round.',
        ].join(' '),
      ),
    () =>
      propose(
        'tokens',
        [
          'CUT THE TOKENS AT UNCHANGED SCORE. You own the cost axis and nothing else.',
          'Start from the leanest frontier point unless the evidence argues for another parent.',
          'Find waste, not work: turns that repeat themselves, nodes whose output nobody reads, context handed to a node',
          'that does not need it, retries that never change the answer, verbose instructions that buy nothing. Read the',
          'journals to distinguish a turn that changed the outcome from a turn that merely happened.',
          'The score must hold. A change that saves tokens by giving up answers is a regression, not a win — if your',
          'mechanism risks losing a correct answer, say so in `risk` and explain why the evidence says it will not.',
        ].join(' '),
      ),
  ])

  const proposals =
    `\n\nPROPOSAL A — score axis:\n${JSON.stringify(scoreProposal, null, 1)}` +
    `\n\nPROPOSAL B — token axis:\n${JSON.stringify(tokenProposal, null, 1)}`

  const out = await ctx.agent(t.instruction + proposals, {
    system: [
      'You are the optimizer of an agent workflow (a workflow.js orchestration program).',
      'Your job this round: push the Pareto frontier for the target domain on two axes — score up, tokens down.',
      'These are not ranked. A candidate that scores the same for half the tokens is as real a win as one that scores higher,',
      'and it enters the frontier on its own merit. Tokens are input+output per task, so node count, turn caps,',
      'how much context each node is handed, and how much it is asked to write are all second-axis decisions.',
      '',
      'TWO PROPOSALS ARE ATTACHED to your instruction, one per axis, written by proposers who each saw only their own',
      'mandate. They are evidence and argument, not orders. You ship ONE candidate this round, and it is yours: adopt one,',
      'combine them where they compose cleanly, take a mechanism from one and drop its integration, or reject both and do',
      'something the evidence supports better. Verify their citations before you trust them — a proposer that could not',
      'probe may have mis-read a journal. State in your notes which proposal(s) you took and why you rejected what you did.',
      'Two mechanisms that both touch the same node usually conflict; prefer shipping one cleanly over merging both badly.',
      '',
      'THE ACTION SPACE — everything a workflow.js can express (full reference: evidence/DSL.md in the opt root):',
      '- Module shape: `export const meta = { name }` + `export default async function run(ctx)`.',
      '- ctx.agent(prompt, {system, model, tools, maxTurns, temperature, thinkingLevel, schema, label, hooks}) — one full agent rollout (a subagent). Every node writes its OWN system prompt and picks its OWN model. Returns final text / schema-validated object / null on failure.',
      '- Orchestration: ctx.pipeline(items, ...stages) (per-item flow, no barrier), ctx.parallel(thunks) (barrier), plain JS glue, ctx.budget for spend introspection. Multi-node structures — decompose→route→assemble, parallel explorers + judge, verify loops — are all expressible.',
      '- Model routing: deepseek-v4-flash is the ONLY model a candidate may select. Asking for any other model makes the node fail with unknown_model. Model choice is therefore NOT a design axis — what varies is topology, per-node system prompts, turn budgets, tools, and rails. A "stronger reviewer" has to be built out of prompt, evidence, and structure, not a bigger model.',
      '- In-loop rails via hooks on any node: preToolUse(call,state)->{block:reason} | postToolUse(call,result,state)->{inject:text} | onTurn(state)->{stop|inject} | onStop(state)->{continue:text, fires when the node tries to finish, max 5}. Hooks may call ctx.agent — an LLM as a rail. state={turn, elapsedMs, tokensSpent, recentCommands, transcript}.',
      '- Structured output: the schema option forces a submit_result tool on the node.',
      '- Domain tools (harness-fixed set per domain; for realmath: python with sympy — see DSL.md).',
      '- Custom tools: ctx.defineTool({name, description, schema, handler}) — handler is your JS and may call ctx.agent (an agent AS a tool) or ctx.runTool(name, args) (compose harness primitives). Pass the handle in the tools array of any node, alongside registry names.',
      '- Hard boundary (not expressible, do not attempt): new side-effect channels (raw network/fs/process), information sources outside the benchmark rules, bypassing token accounting.',
      '',
      'How to work:',
      '- Your context is finite and the evidence is not: the baseline alone is ~29MB across ~200 rollout dirs (journal.jsonl plus a per-node transcript each, median 121KB), and there is no compaction behind you. The attached proposals carry the bulk investigation — the proposers read the raw journals so you do not have to. Read small things yourself (reports, stability.json, notes, workflow sources, earlier candidates) and make targeted read_file dips into specific journals to verify a citation before you build on it.',
      '- The candidate must be a GENERAL orchestration program. The train set is yours to study in full — read its journals, its per-task scores, and the answers your rollouts produced. But what you ship is a program that must work on tasks you have never seen: no branching on task ids, no lookup tables of known answers, no rules fitted to individual problems. Per-task evidence is for inferring the failure MODE; the fix goes in as structure, prompt, routing, or rails that would help an unseen task of that kind.',
      '- Evidence before rules. Any question answerable by reading data (journals, stability report, past notes, diffs) must be answered that way, not by intuition.',
      '- Before editing, write down in your notes: what failure pattern you are targeting (cite specific tasks/journal locations), your hypothesis, and a concrete prediction (which tasks should flip, expected token change).',
      '- Failures have two possible shapes: an existing node did its job badly, OR a needed node does not exist. Consider both, and put one counterfactual to every failure cluster: did the execution ever contain enough useful work to succeed? If no — the gap is capability or coverage. If yes — trace why that work did not reach the final answer before asking any node to redo it. For the second kind, check the historical trajectories first — if the pattern repeats across tasks, the evidence is already there.',
      '- Organization is the search space: how work is decomposed, who does what, what flows between nodes, where results are verified and rescued. Explore organizational designs as freely as prompt wording — the explicit workflow representation exists to make cooperation designable.',
      '- A structural change is judged at the mechanism level: after probing, read the journals and check whether the new node achieved its OWN sub-goal, separately from the total score. Mechanism works but integration is rough → refine integration. Mechanism itself failed → abandon and record why.',
      '- The stability report tells you which tasks oscillate across repeated runs. Read it and weigh evidence accordingly.',
      '- Use run_probe deliberately (it is slow and costs real money): small samples to test a hypothesis, not to grind. Probe the tasks your change actually targets by passing task_ids — the frontier report lists every train task id with its score. Without task_ids you always get the same first 12 tasks, so a fix aimed anywhere else ships completely untested; say so explicitly in your notes if you could not test the target mechanism.',
      '',
      'Hard interface:',
      '- write_workflow is the ONLY way to emit your candidate; it validates before landing. If validation fails, fix and retry.',
      '- write_notes is your only memory across rounds; you will re-read it cold next round. Curate it: beliefs with sources, hypotheses tried and their outcomes, prediction-vs-actual reconciliation from last round.',
      '- Finish by submitting the structured summary.',
    ].join('\n'),
    model: t.opt_model || 'gpt-5.6-terra',
    thinkingLevel: t.opt_thinking || 'xhigh',
    tools: ['read_file', 'list_dir', 'write_workflow', 'write_notes', 'run_probe'],
    maxTurns: 200,  // HARD_MAX_TURNS; sustainable because proposals and small files keep per-turn context cheap
    temperature: 0.2,
    schema: {
      type: 'object',
      properties: {
        made_candidate: { type: 'boolean' },
        hypothesis: { type: 'string' },
        predictions: { type: 'string', description: 'which tasks should flip and expected token delta' },
        proposals_used: {
          type: 'string',
          description: 'which of the two proposals you adopted, combined or rejected, and why — so the next round can tell a bad proposal from a bad integration',
        },
        summary: { type: 'string' },
      },
      required: ['made_candidate', 'hypothesis', 'summary', 'proposals_used'],
    },
    label: 'optimize',
  })
  // The lead can die on its own (context overflow, maxTurns, a transport error) while the
  // workflow itself returns cleanly — this very fallback is what makes it look clean, and
  // run.ts then records status "ok". bcplus v2 round 8 passed for a healthy round for
  // exactly that reason. `node_failed` is the flag the driver's health predicates read, so
  // a dead lead is a watchdog event instead of a silently ordinary round.
  return out || {
    made_candidate: false,
    node_failed: true,
    hypothesis: '',
    proposals_used: '',
    summary: 'optimizer lead node returned nothing (context overflow, maxTurns, or transport error) — see its node_end status in the journal',
  }
}
