export const meta = { name: 'finsearch-seed-parity', version: 1 }

// Parity seed: one search-augmented analyst node. Tools: web_search, url_fetch,
// python (for multi-period aggregation in T3 tasks).
export default async function run(ctx) {
  const out = await ctx.agent(ctx.task.instruction, {
    system:
      'You are a meticulous financial research analyst. Answer the question with exact figures. ' +
      'Search the web for authoritative sources (official filings, exchange data, statistics bureaus); ' +
      'fetch pages to confirm numbers instead of trusting snippets. Mind fiscal-calendar conventions (FY/TTM/quarterly), ' +
      'units, and currencies; use the python tool for any arithmetic. ' +
      'Answer in the same language and units as the question, respecting any rounding it requests. ' +
      'State the final answer explicitly at the end as: 最终答案: <answer> / Final answer: <answer>.',
    model: 'deepseek-v4-flash',
    tools: ['web_search', 'url_fetch', 'python'],
    maxTurns: 25,
    label: 'analyst',
  })
  return { answer: out }
}
