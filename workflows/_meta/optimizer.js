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

  const out = await ctx.agent(t.instruction, {
    system: [
      'You are the optimizer of an agent workflow (a workflow.js orchestration program).',
      'Your job this round: improve the frontier workflow for the target domain — higher score first, fewer tokens second.',
      '',
      'THE ACTION SPACE — everything a workflow.js can express (full reference: evidence/DSL.md in the opt root):',
      '- Module shape: `export const meta = { name }` + `export default async function run(ctx)`.',
      '- ctx.agent(prompt, {system, model, tools, maxTurns, temperature, thinkingLevel, schema, label, hooks}) — one full agent rollout (a subagent). Every node writes its OWN system prompt and picks its OWN model. Returns final text / schema-validated object / null on failure.',
      '- Orchestration: ctx.pipeline(items, ...stages) (per-item flow, no barrier), ctx.parallel(thunks) (barrier), plain JS glue, ctx.budget for spend introspection. Multi-node structures — decompose→route→assemble, parallel explorers + judge, verify loops — are all expressible.',
      '- Model routing (locked two-tier roster): deepseek-v4-flash (cheap execution tier — solving, parallel exploration, light checks) and deepseek-v4-pro (strong reasoning tier — decomposition, hard verification, judging). Each node picks one; the flash/pro split IS a design axis (e.g. pro decomposes, flash executes subtasks).',
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
      '- Use run_probe deliberately (it is slow and costs real money): small samples to test a hypothesis, not to grind.',
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
        summary: { type: 'string' },
      },
      required: ['made_candidate', 'hypothesis', 'summary'],
    },
    label: 'optimize',
  })
  return out || { made_candidate: false, hypothesis: '', summary: 'optimizer node failed' }
}
