"""FinSearchComp grader: the official per-task LLM-judge protocol.

Uses each task's own judge_system_prompt + judge_prompt_template (shipped in the
dataset) and parses {"answer_score": 0|1} exactly like the official eval.py.
Judge endpoint: JUDGE_* env vars, falling back to the gpugeek solver endpoint
with qwen-plus (API-judge decision; calibrate vs official judge on a sample).
"""

from __future__ import annotations

import json
import os
import re
import urllib.request


def _judge_config() -> tuple[str, str, str]:
    base = os.environ.get("JUDGE_BASE_URL") or os.environ.get("SOLVER_BASE_URL", "https://api.gpugeek.com/v1")
    key = os.environ.get("JUDGE_API_KEY") or os.environ.get("SOLVER_API_KEY", "")
    model = os.environ.get("JUDGE_MODEL", "Vendor3/qwen-plus")
    return base, key, model


def _call_judge(system: str, user: str) -> str:
    base, key, model = _judge_config()
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.0,
        "max_tokens": 2048,
    }
    req = urllib.request.Request(
        f"{base.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.load(resp)
    return body["choices"][0]["message"]["content"]


def _parse_score(judge_output: str) -> float | None:
    # official parser: find the JSON block with answer_score (possibly nested)
    for m in re.finditer(r"\{[^{}]*\"answer_score\"[^{}]*\}", judge_output):
        try:
            value = json.loads(m.group(0))["answer_score"]
            while isinstance(value, list):
                value = value[0]
            return float(value)
        except Exception:
            continue
    return None


def grade_task(prediction: object, task: dict) -> tuple[bool, str]:
    if prediction is None or str(prediction).strip() == "":
        return False, "no_answer"
    user = task["judge_template"].format(
        prompt=task["instruction"], response_reference=task["gold"], response=str(prediction)
    )
    for attempt in range(3):
        try:
            out = _call_judge(task["judge_system"], user)
        except Exception:
            continue
        score = _parse_score(out)
        if score is not None:
            return score >= 1.0, "judge"
    return False, "judge_error"
