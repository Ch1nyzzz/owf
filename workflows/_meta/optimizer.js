export const meta = { name: 'owf-optimizer', version: 2 }

// L0 optimizer: one strong agent with privileged tools, free to investigate and edit.
// Principles live in the system prompt; noise facts live in the DATA it reads
// (stability report). No case law, no quota rules.
//
// v2: reader subagents (OPTIMIZER_PLAN §三 "证据太长时才用编排分诊"). The evidence
// surface is ~29MB (198 rollouts x journal + node transcripts) against a 272k window,
// so the lead cannot read it directly — 22 turns of full-size read_file calls fill the
// context, and there is no compaction anywhere in the stack (§四 forbids it on purpose):
// overflow is an API error that kills the round. Dispatching readers moves the bulk into
// child contexts and keeps the lead's turns cheap. It stays a TOOL rather than a fixed
// triage->synthesize pipeline so the optimizer still decides what to investigate.
export default async function run(ctx) {
  const t = ctx.task
  const readerModel = t.opt_model || 'gpt-5.6-terra'

  const investigate = ctx.defineTool({
    name: 'investigate',
    description:
      'Dispatch a reader subagent over the evidence. It reads what you point it at and returns ONLY its findings — ' +
      'the raw file contents never enter your context. Use it for anything bulky: sweeping rollout journals, ' +
      'characterising a failure cluster, comparing how many tasks share a pattern. Ask one focused question per call; ' +
      'you can dispatch several in one turn to cover different clusters.',
    schema: {
      type: 'object',
      properties: {
        question: {
          type: 'string',
          description: 'What to find out, and what to report back. Be specific — the reader cannot ask you follow-ups.',
        },
        paths: {
          type: 'array',
          items: { type: 'string' },
          description: 'Files or directories to start from (in-scope paths).',
        },
      },
      required: ['question'],
    },
    // No schema on the reader: a schema node that hits maxTurns returns null and the
    // whole read is lost, whereas a text node returns whatever it last said.
    handler: async ({ question, paths }) => {
      const out = await ctx.agent(
        `${question}\n\nStart from:\n${(paths || []).join('\n') || '(no paths given — locate them yourself with list_dir)'}`,
        {
          system: [
            'You are a forensic reader serving the optimizer of an agent workflow.',
            'Read the evidence you are pointed at (read_file, list_dir) and report FINDINGS ONLY:',
            'concrete observations with file citations, counts, and the patterns you can support. State how many',
            'cases you actually examined. If the evidence does not answer the question, say so plainly —',
            'a clean negative is worth more than a guess.',
            'No recommendations, no design ideas: your caller does that part.',
            'Budget discipline: read_file returns at most 48KB per call and your context is finite.',
            'Sample deliberately rather than reading everything, and once you are running low on turns,',
            'STOP reading and write your findings — a report cut short still lands, an unwritten one does not.',
            'Be dense. Your caller pays context for every token you return.',
          ].join('\n'),
          model: readerModel,
          thinkingLevel: 'medium',
          tools: ['read_file', 'list_dir'],
          maxTurns: 30,
          label: 'investigate',
        },
      )
      return out || '(reader returned nothing — it may have run out of turns before writing findings)'
    },
  })

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
    'Read small things directly (reports, stability.json, notes, workflow sources) and send `investigate` readers at',
    'anything bulky: rollout journals run to hundreds of KB and would exhaust your context.',
    'Your code_sketch must fit the DSL (evidence/DSL.md) and use only deepseek-v4-flash.',
    'Propose ONE mechanism, the one your evidence supports best — not a menu. Be specific enough to implement.',
  ]

  const propose = (axis, brief) =>
    ctx.agent(`${t.instruction}\n\nYOUR AXIS THIS ROUND:\n${brief}`, {
      system: [...proposerCommon, '', `YOUR AXIS: ${axis}`, brief].join('\n'),
      model: readerModel,
      thinkingLevel: 'high',
      tools: ['read_file', 'list_dir', investigate],
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
          'wrong or absent. Then propose the structure, prompt, routing or rail that would fix that failure MODE on unseen',
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
      '- Your context is finite and the evidence is not: the baseline alone is ~29MB across ~200 rollout dirs (journal.jsonl plus a per-node transcript each, median 121KB). Reading that yourself is impossible — a couple of dozen full-size read_file calls fill your window, and there is no compaction: you would simply die mid-round. So read small things directly (reports, stability.json, notes, workflow sources) and send `investigate` readers at everything bulky. Dispatch several in one turn when you have several questions. Their reading is free to you; only their findings cost you context.',
      '- The candidate must be a GENERAL orchestration program. The train set is yours to study in full — read its journals, its per-task scores, and the answers your rollouts produced. But what you ship is a program that must work on tasks you have never seen: no branching on task ids, no lookup tables of known answers, no rules fitted to individual problems. Per-task evidence is for inferring the failure MODE; the fix goes in as structure, prompt, routing, or rails that would help an unseen task of that kind.',
      '- Evidence before rules. Any question answerable by reading data (journals, stability report, past notes, diffs) must be answered that way, not by intuition.',
      '- Before editing, write down in your notes: what failure pattern you are targeting (cite specific tasks/journal locations), your hypothesis, and a concrete prediction (which tasks should flip, expected token change).',
      '- Failures have two possible shapes: an existing node did its job badly, OR a needed node does not exist. Consider both. For the second kind, check the historical trajectories first — if the pattern repeats across tasks, the evidence is already there.',
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
    tools: ['read_file', 'list_dir', 'write_workflow', 'write_notes', 'run_probe', investigate],
    maxTurns: 200,  // HARD_MAX_TURNS; reachable only because readers keep per-turn context small
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
  return out || { made_candidate: false, hypothesis: '', summary: 'optimizer node failed' }
}
