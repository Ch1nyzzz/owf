export const meta = { name: 'bcplus-seed-parity', version: 1 }

// Parity seed: one deep-search node over the fixed corpus.
export default async function run(ctx) {
  const out = await ctx.agent(ctx.task.instruction, {
    system:
      'You are a persistent research agent working over a fixed document corpus. ' +
      'The answer exists in the corpus. Decompose the clues, search with varied keyword combinations ' +
      '(entities, dates, rare phrases), and open promising documents to verify every criterion. ' +
      'If a search returns nothing useful, reformulate rather than give up. ' +
      'End with the exact answer as: Final answer: <answer>.',
    model: 'deepseek-v4-flash',
    tools: ['search', 'open_doc'],
    maxTurns: 30,
    label: 'searcher',
  })
  return { answer: out }
}
