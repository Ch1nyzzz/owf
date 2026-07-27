"""Reflection LM for the GEPA baseline arm, served through the codex CLI.

The comparison protocol wants GEPA's reflection model to be the SAME model the
main arm's optimizer runs on (gpt-5.6-terra), and the codex subscription is the
channel that model is paid through — a raw API key for it would be a separate,
unfunded spend. GEPA only needs `__call__(prompt) -> str` (its LanguageModel
protocol), so a subprocess wrapper is the whole integration.

Every call is persisted under <log_dir>/call_<n>.{prompt,out}.txt: the
reflection chain is part of the arm's evidence trail, same discipline as the
main arm's journals.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path


class CodexReflectionLM:
    def __init__(self, log_dir: Path, model: str = "gpt-5.6-terra", timeout_sec: int = 900):
        self.model = model
        self.timeout_sec = timeout_sec
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.calls = 0

    def __call__(self, prompt) -> str:
        if isinstance(prompt, list):  # messages format -> flat text
            prompt = "\n\n".join(f"[{m.get('role', 'user')}]\n{m.get('content', '')}" for m in prompt)
        self.calls += 1
        n = self.calls
        (self.log_dir / f"call_{n:03d}.prompt.txt").write_text(prompt)
        out_file = self.log_dir / f"call_{n:03d}.out.txt"

        last_err = ""
        for attempt in range(3):
            t0 = time.time()
            proc = subprocess.run(
                [
                    "codex", "exec",
                    "-m", self.model,
                    "-s", "read-only",
                    "--ephemeral",
                    "--skip-git-repo-check",
                    "--color", "never",
                    "-o", str(out_file),
                    "-",
                ],
                input=prompt, capture_output=True, text=True, timeout=self.timeout_sec,
            )
            text = out_file.read_text().strip() if out_file.exists() else ""
            (self.log_dir / "calls.jsonl").open("a").write(json.dumps({
                "call": n, "attempt": attempt, "rc": proc.returncode,
                "prompt_chars": len(prompt), "out_chars": len(text),
                "sec": round(time.time() - t0, 1),
            }) + "\n")
            if proc.returncode == 0 and text:
                return text
            last_err = proc.stderr[-500:]
            time.sleep(10)
        raise RuntimeError(f"codex reflection failed after 3 attempts: {last_err}")
