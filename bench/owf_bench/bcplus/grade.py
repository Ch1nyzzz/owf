"""BrowseComp-Plus grader: the official semantic-equivalence judge protocol.

GRADER_TEMPLATE ported verbatim from texttron/BrowseComp-Plus
scripts_evaluation/evaluate_run.py (official judge: Qwen3-32B; we use the
API-judge decision — calibrate against the official judge on a sample).
Parses the `correct: yes|no` line.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request

GRADER_TEMPLATE = """
Judge whether the following [response] to [question] is correct or not based on the precise and unambiguous [correct_answer] below.

[question]: {question}

[response]: {response}

[correct_answer]: {correct_answer}

Your judgement must be in the format and criteria specified below:

extracted_final_answer: The final exact answer extracted from the [response].

[correct_answer]: Repeat the [correct_answer] given above.

reasoning: Explain why the extracted_final_answer is correct or incorrect based on [correct_answer], in the context of this [question]. You should judge whether the extracted_final_answer is semantically equivalent to [correct_answer], allowing the extracted_final_answer to be string variations of [correct_answer]. You should also allow the extracted_final_answer to be more precise or verbose than [correct_answer], as long as its additional details are correct. Do not comment on any background to the problem, do not attempt to solve the problem, do not argue for any answer different than [correct_answer], focus only on whether the answers are semantically equivalent.

correct: Answer 'yes' if extracted_final_answer matches the [correct_answer] given above, or is within a small margin of error for numerical problems. Answer 'no' otherwise, i.e. if there if there is any inconsistency, ambiguity, non-equivalency, or if the extracted answer is incorrect.
""".strip()


def _call_judge(user: str) -> str:
    base = os.environ.get("JUDGE_BASE_URL") or os.environ.get("SOLVER_BASE_URL", "https://api.gpugeek.com/v1")
    key = os.environ.get("JUDGE_API_KEY") or os.environ.get("SOLVER_API_KEY", "")
    model = os.environ.get("JUDGE_MODEL", "Vendor3/qwen-plus")
    payload = {"model": model, "messages": [{"role": "user", "content": user}], "temperature": 0.0, "max_tokens": 2048}
    req = urllib.request.Request(
        f"{base.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)["choices"][0]["message"]["content"]


def grade_task(prediction: object, task: dict) -> tuple[bool, str]:
    if prediction is None or str(prediction).strip() == "":
        return False, "no_answer"
    user = GRADER_TEMPLATE.format(question=task["instruction"], response=str(prediction), correct_answer=task["gold"])
    for _ in range(3):
        try:
            out = _call_judge(user)
        except Exception:
            continue
        m = re.search(r"correct\s*:\s*(yes|no)", out, re.IGNORECASE)
        if m:
            return m.group(1).lower() == "yes", "judge"
    return False, "judge_error"
