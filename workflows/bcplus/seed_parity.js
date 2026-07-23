export const meta = { name: 'bcplus-seed-parity', version: 2 }

// Parity seed: one deep-search node over the fixed corpus (v2: maxTurns raised
// 30->64 plus an answer-before-exhaustion instruction, after the probe50 sample
// showed the seed never answers wrongly — it only runs out of resources. Of 13
// completed rollouts, all 8 that finished normally graded correct, while 5 hit
// the turn cap or the token ceiling and returned null. An honest baseline has to
// let the model finish; see OPTIMIZER_PLAN §六).
export default async function run(ctx) {
  const out = await ctx.agent(ctx.task.instruction, {
    system:
      'You are a persistent research agent working over a fixed document corpus. ' +
      'The answer exists in the corpus. Decompose the clues, search with varied keyword combinations ' +
      '(entities, dates, rare phrases), and open promising documents to verify every criterion. ' +
      'If a search returns nothing useful, reformulate rather than give up. ' +
      'You have a limited number of turns: track how many you have used, and once you are running low, ' +
      'stop searching and commit to your best current candidate — an unverified best guess scores far ' +
      'better than no answer at all. Never end without an answer. ' +
      'End with the exact answer as: Final answer: <answer>.',
    model: 'deepseek-v4-flash',
    tools: ['search', 'open_doc'],
    maxTurns: 64,
    label: 'searcher',
  })
  return { answer: out }
}
