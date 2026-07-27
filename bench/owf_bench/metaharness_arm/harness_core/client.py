"""SUT client for the meta-harness arm — READ-ONLY for the proposer.

This module is part of the frozen core (the analogue of meta-harness's
claude_wrapper.py / meta_harness.py, which its SKILL forbids editing): token
accounting and the budget gate live here, so an editable copy would be a
cheating channel. Candidates receive an instance; they never construct one.

Accounting parity with the main arm: cache hits are billed at the full input
rate (executor/src/budget.ts does the same, because the solver endpoint reports
cached_tokens erratically) — prompt_tokens is used as-is.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request


class BudgetExceeded(Exception):
    pass


class FlashClient:
    """OpenAI-compatible chat client pinned to the SUT model (deepseek-v4-flash)."""

    MODEL_ID = "Vendor3/DeepSeek-V4-Flash"
    MAX_COMPLETION_TOKENS = 32768  # model registry cap (configs/models.yaml)

    def __init__(self, token_budget: int):
        self.base_url = os.environ.get("SOLVER_BASE_URL", "https://api.gpugeek.com/v1").rstrip("/")
        self.api_key = os.environ.get("SOLVER_API_KEY", "")
        self.token_budget = token_budget
        self.tokens = {"input": 0, "output": 0}

    def spent(self) -> int:
        return self.tokens["input"] + self.tokens["output"]

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             temperature: float = 0.0) -> dict:
        """One chat completion. Returns the assistant message dict (may carry tool_calls).

        Raises BudgetExceeded BEFORE the call once the per-task budget is spent —
        same semantics as the executor (the node fails, the harness records why).
        """
        if self.spent() >= self.token_budget:
            raise BudgetExceeded(f"token budget exhausted: {self.spent()}/{self.token_budget}")
        payload: dict = {
            "model": self.MODEL_ID,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": self.MAX_COMPLETION_TOKENS,
        }
        if tools:
            payload["tools"] = tools
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
        )
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=600) as resp:
                    data = json.loads(resp.read())
                usage = data.get("usage", {})
                self.tokens["input"] += int(usage.get("prompt_tokens", 0))
                self.tokens["output"] += int(usage.get("completion_tokens", 0))
                return data["choices"][0]["message"]
            except Exception as e:  # transport/5xx; the caller cannot fix these
                last_err = e
                time.sleep(5 * (attempt + 1))
        raise RuntimeError(f"solver call failed after 3 attempts: {last_err}")
