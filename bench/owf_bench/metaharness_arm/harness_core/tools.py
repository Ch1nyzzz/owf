"""Tool primitives for the meta-harness arm — READ-ONLY for the proposer.

Faithful ports of the executor's harness-owned tools (executor/src/tools/*.ts):
same timeout, same output caps, same result phrasing. Tool *composition* is the
candidate's business; the primitives are the comparability contract.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import urllib.parse
import urllib.request

PY_OUTPUT_LIMIT = 16 * 1024
PY_TIMEOUT_SEC = 15  # python.ts: long calls are sympy stuck, not progress

PYTHON_TOOL = {
    "type": "function",
    "function": {
        "name": "python",
        "description": "Execute a self-contained Python script and return its stdout/stderr. "
                       "sympy is available. State does NOT persist between calls; print() what you need to see.",
        "parameters": {
            "type": "object",
            "properties": {"code": {"type": "string", "description": "Python source to execute"}},
            "required": ["code"],
        },
    },
}

SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "search",
        "description": "Keyword-search the fixed document corpus (BM25). Returns docids with titles and "
                       "snippets; read full documents with open_doc.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "k": {"type": "number"}},
            "required": ["query"],
        },
    },
}

OPEN_DOC_TOOL = {
    "type": "function",
    "function": {
        "name": "open_doc",
        "description": "Read a corpus document in full by its docid.",
        "parameters": {
            "type": "object",
            "properties": {"docid": {"type": "string"}},
            "required": ["docid"],
        },
    },
}


def _truncate(s: str) -> str:
    return s if len(s) <= PY_OUTPUT_LIMIT else f"{s[:PY_OUTPUT_LIMIT]}\n…[truncated {len(s) - PY_OUTPUT_LIMIT} bytes]"


def run_python(code: str) -> str:
    import os
    workdir = tempfile.mkdtemp(prefix="owf-meta-py-")
    interpreter = os.environ.get("OWF_PYTHON", "python3")  # same resolution as python.ts
    try:
        proc = subprocess.run([interpreter, "-c", code], cwd=workdir, capture_output=True,
                              text=True, timeout=PY_TIMEOUT_SEC,
                              env={"PATH": os.environ.get("PATH", ""), "HOME": workdir})
        parts = []
        if proc.stdout:
            parts.append(_truncate(proc.stdout))
        if proc.stderr:
            parts.append(f"[stderr]\n{_truncate(proc.stderr)}")
        if proc.returncode != 0 and not proc.stdout and not proc.stderr:
            parts.append(f"[error] exit code {proc.returncode}")
        return "\n".join(parts) or "(no output)"
    except subprocess.TimeoutExpired as e:
        parts = []
        if e.stdout:
            parts.append(_truncate(e.stdout if isinstance(e.stdout, str) else e.stdout.decode()))
        parts.append(
            f"[timeout] killed after {PY_TIMEOUT_SEC}s. This computation will not finish as written — "
            "do not retry it unchanged. Narrow it (smaller case, closed form, a different sympy entry point) "
            "or reason it out directly."
        )
        return "\n".join(parts)


def _bcplus_base() -> str:
    import os
    return os.environ.get("OWF_BCPLUS_SERVER", "http://127.0.0.1:8931").rstrip("/")


def run_search(query: str, k: int | None = None) -> str:
    k = min(int(k or 8), 20)
    url = f"{_bcplus_base()}/search?q={urllib.parse.quote(query)}&k={k}"
    with urllib.request.urlopen(url, timeout=60) as resp:
        rows = json.loads(resp.read())
    text = "\n".join(
        f"{i + 1}. [{r['docid']}] {r['title']}\n   {' '.join(r['snippet'].split())[:300]}"
        for i, r in enumerate(rows)
    )
    return text or "(no results)"


def run_open_doc(docid: str) -> str:
    url = f"{_bcplus_base()}/doc?id={urllib.parse.quote(docid)}"
    with urllib.request.urlopen(url, timeout=60) as resp:
        data = json.loads(resp.read())
    if data.get("error"):
        return data["error"]
    return f"URL: {data.get('url')}\n\n{data.get('text')}"


DISPATCH = {
    "python": lambda args: run_python(args["code"]),
    "search": lambda args: run_search(args["query"], args.get("k")),
    "open_doc": lambda args: run_open_doc(args["docid"]),
}
