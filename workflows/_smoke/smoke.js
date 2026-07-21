export const meta = { name: 'smoke-test', version: 1 }

export default async function run(ctx) {
  ctx.log('smoke: single python-tool node with schema output and an onStop rail')
  const out = await ctx.agent(
    `Compute the exact value of ${ctx.task.instruction}. Use the python tool to verify before submitting.`,
    {
      system: 'You are a careful calculator. Always verify numerically with the python tool before answering.',
      model: 'deepseek-v4-flash',
      tools: ['python'],
      maxTurns: 8,
      schema: { type: 'object', properties: { answer: { type: 'number' } }, required: ['answer'] },
      label: 'calc',
      hooks: {
        onStop: (state) => {
          if (state.turn < 2) return { continue: 'Did you verify with python? If not, do it now, then submit.' }
        },
      },
    },
  )
  return { answer: out ? out.answer : null, tokensSpent: ctx.budget.spentTokens() }
}
