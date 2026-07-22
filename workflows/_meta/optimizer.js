export const meta = { name: 'owf-optimizer', version: 1 }

// L0 optimizer: one strong agent with privileged tools, free to investigate and edit.
// Principles live in the system prompt; noise facts live in the DATA it reads
// (stability report). No case law, no quota rules.
export default async function run(ctx) {
  const t = ctx.task
  const out = await ctx.agent(t.instruction, {
    system: [
      'You are the optimizer of an agent workflow (a workflow.js orchestration program).',
      'Your job this round: improve the frontier workflow for the target domain — higher score first, fewer tokens second.',
      '',
      'How to work:',
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
    model: t.opt_model || 'kimi-k2',
    tools: ['read_file', 'list_dir', 'write_workflow', 'write_notes', 'run_probe'],
    maxTurns: 80,
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
