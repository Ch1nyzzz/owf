export const meta = { name: 'realmath-seed-parity', version: 2 }

// Parity seed: one solver node with the python tool, mirroring a plain
// tool-augmented single-agent setup at an honest configuration (v2: maxTurns
// raised 20->64 after the k1 baseline showed 47% of tasks dying on turn
// exhaustion — the baseline must reflect the model's normal level).
export default async function run(ctx) {
  const parts = ctx.task.answer_kind === 'multi' ? 'This problem has MULTIPLE answer parts; submit one string per part, in order.' : 'Submit a single-element list.'
  const out = await ctx.agent(ctx.task.instruction, {
    system:
      'You are a research mathematician. Solve the problem exactly. ' +
      'Use the python tool (sympy is available) to compute and verify your answer numerically or symbolically before submitting. ' +
      'Submit exact symbolic expressions (SymPy-parseable strings), never decimal approximations. ' +
      parts + ' ' +
      'Keep written reasoning brief; put computations in python. If you cannot fully verify, still submit your best answer before running out of turns.',
    model: 'deepseek-v4-flash',
    tools: ['python'],
    maxTurns: 64,
    schema: {
      type: 'object',
      properties: { answer: { type: 'array', items: { type: 'string' } } },
      required: ['answer'],
    },
    label: 'solve',
  })
  return { answer: out ? out.answer : null }
}
