"""RealMath grader: exact string match first, then SymPy symbolic equivalence.

Modeled on the two-tier scheme described in arXiv:2607.06820 (parse both sides,
simplify the difference; zero difference = correct). Own implementation.
"""

from __future__ import annotations

from typing import Any


def _string_equal(pred: str, ref: str) -> bool:
    pred, ref = pred.strip(), ref.strip()
    if pred == ref:
        return True
    try:
        import sympy
        from sympy.parsing.sympy_parser import parse_expr

        try:
            lhs = parse_expr(pred)
            rhs = parse_expr(ref)
        except Exception:
            lhs = sympy.sympify(pred)
            rhs = sympy.sympify(ref)
        diff = sympy.simplify(lhs - rhs)
        return bool(diff == 0)
    except Exception:
        return False


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    s = str(value).strip()
    # gold sympy_answer is stored either as a real list or as a python-repr string
    if s.startswith("[") and s.endswith("]"):
        try:
            import ast

            parsed = ast.literal_eval(s)
            if isinstance(parsed, list):
                return [str(v) for v in parsed]
        except Exception:
            pass
    return [s]


def _judge_equivalent(question: str, prediction: list[str], gold: list[str]) -> bool | None:
    """LLM-judge fallback, mirroring arXiv:2607.06820's role for the judge: recover
    answers that are mathematically equivalent but differ in symbol names or canonical
    form. Only called when the symbolic check failed. Returns None if the judge is
    unavailable (missing key / API error) so callers can keep the symbolic verdict."""
    import os
    import urllib.request

    base = os.environ.get("SOLVER_BASE_URL", "https://api.gpugeek.com/v1")
    key = os.environ.get("SOLVER_API_KEY")
    if not key:
        return None
    prompt = (
        "You verify mathematical answer equivalence. Question (for context):\n"
        f"{question[:4000]}\n\n"
        f"Reference answer (list of parts): {gold}\n"
        f"Candidate answer (list of parts): {prediction}\n\n"
        "Are the candidate parts mathematically EQUIVALENT to the reference parts (same order)? "
        "Different symbol names for the same quantities, or different but equivalent canonical forms, count as equivalent. "
        "A different mathematical object, value, or structure does NOT. "
        'Reply with exactly one word: "equivalent" or "different".'
    )
    body = json.dumps(
        {
            "model": "Vendor3/DeepSeek-V4-Flash",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
            "max_tokens": 2048,
        }
    ).encode()
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        text = data["choices"][0]["message"]["content"].strip().lower()
        if "equivalent" in text and "different" not in text:
            return True
        if "different" in text:
            return False
        return None
    except Exception:
        return None


import json  # noqa: E402  (used by the judge helper)


def grade(prediction: Any, gold: Any, question: str = "") -> tuple[bool, str]:
    """Return (correct, match_type). Prediction is the workflow's submitted answer
    (string or list of strings); gold is the task's sympy_answer."""
    if prediction is None:
        return False, "no_answer"
    pred_list = _as_list(prediction)
    gold_list = _as_list(gold)
    if len(pred_list) != len(gold_list):
        return False, "arity_mismatch"
    if all(p.strip() == g.strip() for p, g in zip(pred_list, gold_list)):
        return True, "exact"
    if all(_string_equal(p, g) for p, g in zip(pred_list, gold_list)):
        return True, "symbolic"
    verdict = _judge_equivalent(question, pred_list, gold_list)
    if verdict is True:
        return True, "judge_recovered"
    return False, "mismatch" if verdict is None else "judge_confirmed_mismatch"


def grade_task(prediction: Any, task: dict) -> tuple[bool, str]:
    return grade(prediction, task["gold"], question=task.get("instruction", ""))
