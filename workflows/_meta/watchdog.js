export const meta = { name: 'owf-watchdog', version: 3 }

// L1 watchdog: hand-written, frozen, single node. Top of the RSI tower — nothing watches it.
// It is invoked ONLY when mechanical health predicates fire (computed by the driver, not by an LLM).
// Three verdicts:
//   healthy_stagnation — process looks sound; likely ceiling/noise. Do nothing; escalate to human.
//   process_pathology  — the optimizer's process is degenerate (must cite concrete evidence). Rewrite optimizer.js.
//   operational_fault  — the optimizer crashes / overruns / produces nothing. Repair optimizer.js.
//
// v2: reader subagents, for the same reason optimizer v2 got them — §五 assumed the
// optimizer's history was "量小,单节点装得下", which stopped being true once the optimizer
// itself started dispatching readers (~400KB per round, so ≥1.2MB by the time the earliest
// predicate can fire). Deliberately NOT compaction: §五's only guardrail is that a pathology
// verdict must cite concrete evidence, and summarised context cannot cite. A reader reads the
// raw journal and reports with paths; a summariser would also insert an unaudited judgement
// directly upstream of the one component nothing else watches.
// The tool is duplicated from optimizer.js rather than shared: sandbox.ts runs workflows as
// vm.Script with require/import banned, so every workflow is a self-contained file.
//
// v3: the rewrite is delivered through write_workflow instead of a `rewrite` field on the
// final verdict. bcplus round 4 diagnosed its optimizer correctly and produced a 15KB
// rewrite, then the response carrying it failed mid-stream — the node returned null and the
// entire intervention was recorded as "watchdog node failed". Staking a round's whole output
// on one 17KB submission has no recovery path. Through the tool, the source is on disk the
// instant it is written and the validation gate answers inline, so a rejected rewrite can be
// retried rather than lost, and the verdict left to deliver is small.
export default async function run(ctx) {
  const t = ctx.task

  const investigate = ctx.defineTool({
    name: 'investigate',
    description:
      'Dispatch a reader subagent over the optimizer history. It reads what you point it at and returns ONLY its ' +
      'findings — the raw journals never enter your context. Use it for anything bulky: a round\'s optimizer journal, ' +
      'the edit history across rounds, how the notes evolved. Ask one focused question per call; you may dispatch several at once.',
    schema: {
      type: 'object',
      properties: {
        question: {
          type: 'string',
          description: 'What to find out, and what to report back. Demand file paths and quotes — your verdict must cite them.',
        },
        paths: { type: 'array', items: { type: 'string' }, description: 'Files or directories to start from (in-scope paths).' },
      },
      required: ['question'],
    },
    // No schema on the reader: a schema node that hits maxTurns returns null and the read is
    // lost, whereas a text node returns whatever it last said.
    handler: async ({ question, paths }) => {
      const out = await ctx.agent(
        `${question}\n\nStart from:\n${(paths || []).join('\n') || '(no paths given — locate them yourself with list_dir)'}`,
        {
          system: [
            'You are a forensic reader serving the watchdog of an optimization loop.',
            'Read the evidence you are pointed at (read_file, list_dir) and report FINDINGS ONLY:',
            'concrete observations with file paths and verbatim quotes, counts, and the patterns you can support.',
            'State how many rounds/files you actually examined. Your caller must cite your findings as evidence for a',
            'verdict about the optimizer, so an unsupported claim from you is worse than a gap — if the evidence does',
            'not answer the question, say so plainly.',
            'No verdicts, no recommendations: your caller decides.',
            'Budget discipline: read_file returns at most 48KB per call and your context is finite.',
            'Sample deliberately rather than reading everything, and once you are running low on turns,',
            'STOP reading and write your findings — a report cut short still lands, an unwritten one does not.',
            'Be dense. Your caller pays context for every token you return.',
          ].join('\n'),
          model: t.opt_model || 'gpt-5.6-terra',
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
      'You are the watchdog of an optimization loop. Mechanical health predicates fired; diagnose the OPTIMIZER (not the domain workflows).',
      'Read the optimizer source, its notes, its per-round journals and the round history. Then decide:',
      '',
      'How to read a history that does not fit: state.json is your index — one mechanical record per round',
      '(candidate_made, candidate_score, candidate_tokens, entered_frontier, optimizer_status, optimizer_tokens,',
      'frontier_after {points, best_score, best_tokens}), a few KB in total. Optimization is two-axis: a candidate',
      'enters the Pareto frontier by scoring higher OR using fewer tokens without losing on the other axis, so a',
      'round that only cut tokens is progress, and a flat best_score alone is not evidence of a stalled optimizer.',
      'Read it and NOTES.md directly, spot the rounds that look wrong, and send `investigate` readers at those rounds\'',
      'journals rather than reading them yourself: a single round of optimizer journal plus its reader transcripts runs to',
      'hundreds of KB and would exhaust your context before you reached a verdict. Ask readers for verbatim quotes and paths —',
      'your verdict is invalid without citations, and their findings are what you cite.',
      '',
      '- healthy_stagnation: the optimizer investigates properly, hypotheses are reasonable, predictions are reconciled — the plateau likely reflects a model ceiling or noise floor. Do NOT touch anything, and do NOT write a file.',
      '- process_pathology: the optimization process itself is degenerate. You must cite concrete evidence (specific rounds, notes entries, edit history). Write a full rewritten optimizer.js.',
      '- operational_fault: the optimizer fails to run properly (context overruns, no candidates, tool misuse loops). Write a full rewritten optimizer.js fixing the fault, changing as little else as possible.',
      'A rewrite without cited evidence is invalid. "A different approach might work better" is not evidence.',
      '',
      'HOW TO DELIVER A REWRITE: call write_workflow with the complete new optimizer.js source. Despite its name it',
      'writes the optimizer replacement, not a domain candidate — the destination is fixed by the harness. It validates',
      'before landing and returns the error inline on failure, so fix and call it again; nothing is written until it passes.',
      'Write it as soon as you have decided, BEFORE composing your verdict: once the file lands it is safe even if your',
      'final message never arrives. Then submit the verdict with a short evidence citation. Do not restate the source in',
      'the verdict — the file is the deliverable and duplicating it risks losing both.',
      'If you rewrite: keep the module shape (`export const meta` + `export default async function run(ctx)`), keep the same tool names, and keep the system prompt principles unless they are the diagnosed problem.',
    ].join('\n'),
    model: t.opt_model || 'gpt-5.6-terra',
    thinkingLevel: t.opt_thinking || 'xhigh',
    tools: ['read_file', 'list_dir', 'write_workflow', investigate],
    maxTurns: 60,
    temperature: 0.0,
    schema: {
      type: 'object',
      properties: {
        verdict: { type: 'string', enum: ['healthy_stagnation', 'process_pathology', 'operational_fault'] },
        evidence: { type: 'string', description: 'cited rounds/files/lines supporting the verdict' },
        rewrite_written: { type: 'boolean', description: 'true if you delivered a rewrite via write_workflow' },
      },
      required: ['verdict', 'evidence'],
    },
    label: 'watchdog',
  })
  // A null here no longer discards an intervention: if the node wrote its rewrite before
  // dying, the driver finds the staged file and applies it regardless of this fallback.
  return out || { verdict: 'healthy_stagnation', evidence: 'watchdog node failed; defaulting to no-op' }
}
