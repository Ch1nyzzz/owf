export const meta = { name: 'realmath-seed-parity', version: 1 }

// Parity seed: one solver node with the python tool, mirroring the plain
// tool-augmented single-agent setup. This is the optimization starting point;
// everything here (structure, prompts, hooks) is candidate material.
export default async function run(ctx) {
  const out = await ctx.agent(ctx.task.instruction, {
    system:
      'You are a research mathematician. Solve the problem exactly. ' +
      'Use the python tool (sympy is available) to compute and verify your answer numerically or symbolically before submitting. ' +
      'Submit exact symbolic expressions (SymPy-parseable strings), never decimal approximations.',
    model: 'deepseek-v4-flash',
    tools: ['python'],
    maxTurns: 20,
    schema: {
      type: 'object',
      properties: { answer: { type: 'array', items: { type: 'string' } } },
      required: ['answer'],
    },
    label: 'solve',
  })
  return { answer: out ? out.answer : null }
}
