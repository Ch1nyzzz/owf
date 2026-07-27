// Baseline agent for realmath: the parity seed, verbatim, on the meta-harness substrate.
// One runAgentNode call with exactly the parameters of workflows/realmath/seed_parity.js —
// the identical code path a workflow ctx.agent() takes, so baseline parity is by
// construction, not by imitation.
//
// Candidates may copy and modify this file under a new name: change the prompt, the
// node parameters, add nodes, add hooks, or drop below runAgentNode onto core.pi
// (pi-agent-core) and rewrite the loop itself. Contract: export solve(task, core).

export async function solve(task, core) {
  const parts = task.answer_kind === 'multi'
    ? 'This problem has MULTIPLE answer parts; submit one string per part, in order.'
    : 'Submit a single-element list.'
  const out = await core.runAgentNode(task.instruction, {
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
  }, 1, core.deps)
  return { answer: out.result ? out.result.answer : null }
}
