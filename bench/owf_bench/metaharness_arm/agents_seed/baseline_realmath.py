"""Baseline agent for realmath: the parity seed translated to plain Python.

This is the arm's "Terminus2" — the base class candidates subclass or replace.
Same system prompt, model, tool, turn cap, and structured-answer contract as
workflows/realmath/seed_parity.js: submit via a forced submit_result tool,
three validation failures fail the task, budget/wallclock exhaustion returns None.

Candidates may override anything here or write a new AgentHarness from scratch;
the runner contract is just:  AgentHarness(client, log).solve(task, deadline)
-> list[str] | None.  (task is the public payload — no gold.)
"""

from __future__ import annotations

import json
import time

from owf_bench.metaharness_arm.harness_core.client import BudgetExceeded, FlashClient
from owf_bench.metaharness_arm.harness_core.tools import DISPATCH, PYTHON_TOOL

SYSTEM = (
    "You are a research mathematician. Solve the problem exactly. "
    "Use the python tool (sympy is available) to compute and verify your answer numerically or symbolically before submitting. "
    "Submit exact symbolic expressions (SymPy-parseable strings), never decimal approximations. "
    "Keep written reasoning brief; put computations in python. "
    "If you cannot fully verify, still submit your best answer before running out of turns."
)

SUBMIT_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_result",
        "description": "Submit the final answer and end the task.",
        "parameters": {
            "type": "object",
            "properties": {"answer": {"type": "array", "items": {"type": "string"}}},
            "required": ["answer"],
        },
    },
}


class AgentHarness:
    name = "baseline_realmath"
    max_turns = 64

    def __init__(self, client: FlashClient, log):
        self.client = client
        self.log = log  # log(dict) appends one event to the task trajectory

    def system_prompt(self, task: dict) -> str:
        parts = ("This problem has MULTIPLE answer parts; submit one string per part, in order."
                 if task.get("answer_kind") == "multi" else "Submit a single-element list.")
        return f"{SYSTEM} {parts} When you are done, call submit_result with your final answer."

    def solve(self, task: dict, deadline: float):
        messages = [
            {"role": "system", "content": self.system_prompt(task)},
            {"role": "user", "content": task["instruction"]},
        ]
        validation_failures = 0
        for turn in range(1, self.max_turns + 1):
            if time.time() > deadline:
                return None
            try:
                msg = self.client.chat(messages, tools=[PYTHON_TOOL, SUBMIT_TOOL])
            except BudgetExceeded:
                return None
            messages.append(msg)
            self.log({"turn": turn, "assistant": msg.get("content"),
                      "tool_calls": [c["function"]["name"] for c in msg.get("tool_calls") or []]})
            calls = msg.get("tool_calls") or []
            if not calls:
                # The contract requires submit_result; push back once per idle turn.
                messages.append({"role": "user", "content": "Call submit_result with your final answer."})
                continue
            for call in calls:
                fn = call["function"]["name"]
                try:
                    args = json.loads(call["function"]["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = None
                if fn == "submit_result":
                    if isinstance(args, dict) and isinstance(args.get("answer"), list) \
                            and all(isinstance(x, str) for x in args["answer"]):
                        return args["answer"]
                    validation_failures += 1
                    if validation_failures >= 3:
                        return None
                    result = "validation error: answer must be a list of strings"
                elif fn in DISPATCH and args is not None:
                    try:
                        result = DISPATCH[fn](args)
                    except Exception as e:  # executor semantics: a throwing handler is an error tool result, not a dead rollout
                        result = f"tool error: {e}"
                else:
                    result = f"unknown tool or bad arguments: {fn}"
                self.log({"turn": turn, "tool": fn, "result_preview": str(result)[:2048]})
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": str(result)})
        return None
