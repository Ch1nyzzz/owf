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


def grade(prediction: Any, gold: Any) -> tuple[bool, str]:
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
    return False, "mismatch"
