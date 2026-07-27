"""Baseline agent for bcplus: the parity seed translated to plain Python.

Same system prompt, model, tools, and turn cap as workflows/bcplus/seed_parity.js.
The final answer is the last assistant text once the model stops calling tools
(the seed workflow returns the node's final text verbatim).

Runner contract: AgentHarness(client, log).solve(task, deadline) -> str | None.
"""

from __future__ import annotations

import json
import time

from owf_bench.metaharness_arm.harness_core.client import BudgetExceeded, FlashClient
from owf_bench.metaharness_arm.harness_core.tools import DISPATCH, OPEN_DOC_TOOL, SEARCH_TOOL

SYSTEM = (
    "You are a persistent research agent working over a fixed document corpus. "
    "The answer exists in the corpus. Decompose the clues, search with varied keyword combinations "
    "(entities, dates, rare phrases), and open promising documents to verify every criterion. "
    "If a search returns nothing useful, reformulate rather than give up. "
    "You have a limited number of turns: track how many you have used, and once you are running low, "
    "stop searching and commit to your best current candidate — an unverified best guess scores far "
    "better than no answer at all. Never end without an answer. "
    "End with the exact answer as: Final answer: <answer>."
)


class AgentHarness:
    name = "baseline_bcplus"
    max_turns = 64

    def __init__(self, client: FlashClient, log):
        self.client = client
        self.log = log

    def solve(self, task: dict, deadline: float):
        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": task["instruction"]},
        ]
        last_text = None
        for turn in range(1, self.max_turns + 1):
            if time.time() > deadline:
                return last_text
            try:
                msg = self.client.chat(messages, tools=[SEARCH_TOOL, OPEN_DOC_TOOL])
            except BudgetExceeded:
                return last_text
            messages.append(msg)
            if msg.get("content"):
                last_text = msg["content"]
            self.log({"turn": turn, "assistant": msg.get("content"),
                      "tool_calls": [c["function"]["name"] for c in msg.get("tool_calls") or []]})
            calls = msg.get("tool_calls") or []
            if not calls:
                return last_text  # the model finished
            for call in calls:
                fn = call["function"]["name"]
                try:
                    args = json.loads(call["function"]["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = None
                if fn in DISPATCH and args is not None:
                    try:
                        result = DISPATCH[fn](args)
                    except Exception as e:
                        result = f"tool error: {e}"
                else:
                    result = f"unknown tool or bad arguments: {fn}"
                self.log({"turn": turn, "tool": fn, "result_preview": str(result)[:2048]})
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": str(result)})
        return last_text
