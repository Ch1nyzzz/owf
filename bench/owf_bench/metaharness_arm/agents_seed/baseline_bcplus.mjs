// Baseline agent for bcplus: the parity seed, verbatim, on the meta-harness substrate.
// See baseline_realmath.mjs for the substrate contract.

export async function solve(task, core) {
  const out = await core.runAgentNode(task.instruction, {
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
  }, 1, core.deps)
  return { answer: out.result }
}
