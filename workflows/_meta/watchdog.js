export const meta = { name: 'owf-watchdog', version: 1 }

// L1 watchdog: hand-written, frozen, single node. Top of the RSI tower — nothing watches it.
// It is invoked ONLY when mechanical health predicates fire (computed by the driver, not by an LLM).
// Three verdicts:
//   healthy_stagnation — process looks sound; likely ceiling/noise. Do nothing; escalate to human.
//   process_pathology  — the optimizer's process is degenerate (must cite concrete evidence). Rewrite optimizer.js.
//   operational_fault  — the optimizer crashes / overruns / produces nothing. Repair optimizer.js.
export default async function run(ctx) {
  const t = ctx.task
  const out = await ctx.agent(t.instruction, {
    system: [
      'You are the watchdog of an optimization loop. Mechanical health predicates fired; diagnose the OPTIMIZER (not the domain workflows).',
      'Read the optimizer source, its notes, its per-round journals and the round history. Then decide:',
      '- healthy_stagnation: the optimizer investigates properly, hypotheses are reasonable, predictions are reconciled — the plateau likely reflects a model ceiling or noise floor. Do NOT touch anything.',
      '- process_pathology: the optimization process itself is degenerate. You must cite concrete evidence (specific rounds, notes entries, edit history). Provide a full rewritten optimizer.js.',
      '- operational_fault: the optimizer fails to run properly (context overruns, no candidates, tool misuse loops). Provide a full rewritten optimizer.js fixing the fault, changing as little else as possible.',
      'A rewrite without cited evidence is invalid. "A different approach might work better" is not evidence.',
      'If you rewrite: keep the module shape (`export const meta` + `export default async function run(ctx)`), keep the same tool names, and keep the system prompt principles unless they are the diagnosed problem.',
    ].join('\n'),
    model: t.opt_model || 'deepseek-v4-pro',
    tools: ['read_file', 'list_dir'],
    maxTurns: 40,
    temperature: 0.0,
    schema: {
      type: 'object',
      properties: {
        verdict: { type: 'string', enum: ['healthy_stagnation', 'process_pathology', 'operational_fault'] },
        evidence: { type: 'string', description: 'cited rounds/files/lines supporting the verdict' },
        rewrite: { type: 'string', description: 'full new optimizer.js source; empty string if verdict is healthy_stagnation' },
      },
      required: ['verdict', 'evidence'],
    },
    label: 'watchdog',
  })
  return out || { verdict: 'healthy_stagnation', evidence: 'watchdog node failed; defaulting to no-op' }
}
