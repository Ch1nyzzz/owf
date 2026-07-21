"""Local retrieval server for BrowseComp-Plus (stdlib only).

GET /search?q=...&k=8  -> JSON [{docid, title_line, score, snippet}]
GET /doc?id=...        -> JSON {docid, url, text}

The fixed corpus + this server is what makes the domain reproducible: the agent
never touches the live web.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[3]
DB = ROOT / "data/bcplus/docs.db"
PORT = int(__import__("os").environ.get("OWF_BCPLUS_PORT", "8931"))

_local = threading.local()


def conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn"):
        _local.conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    return _local.conn


def fts_query(q: str) -> str:
    # quote each term: user queries contain FTS5 operators otherwise
    terms = re.findall(r"\w+", q)
    return " ".join(f'"{t}"' for t in terms[:32]) or '""'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def send_json(self, obj: object, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        params = parse_qs(u.query)
        try:
            if u.path == "/search":
                q = params.get("q", [""])[0]
                k = min(int(params.get("k", ["8"])[0]), 20)
                rows = conn().execute(
                    "SELECT d.docid, d.text, bm25(docs_fts) AS score FROM docs_fts JOIN docs d ON d.rowid = docs_fts.rowid "
                    "WHERE docs_fts MATCH ? ORDER BY score LIMIT ?",
                    (fts_query(q), k),
                ).fetchall()
                out = []
                for docid, text, score in rows:
                    first = text.strip().split("\n")
                    title = next((l for l in first if l.strip() and not l.startswith("---")), "")[:150]
                    out.append({"docid": docid, "title": title, "score": round(score, 3), "snippet": text[:500]})
                self.send_json(out)
            elif u.path == "/doc":
                docid = params.get("id", [""])[0]
                row = conn().execute("SELECT docid, url, text FROM docs WHERE docid = ?", (docid,)).fetchone()
                if row is None:
                    self.send_json({"error": f"no such docid: {docid}"}, 404)
                else:
                    self.send_json({"docid": row[0], "url": row[1], "text": row[2][:60000]})
            elif u.path == "/health":
                self.send_json({"ok": True})
            else:
                self.send_json({"error": "unknown path"}, 404)
        except Exception as e:  # noqa: BLE001
            self.send_json({"error": str(e)}, 500)


if __name__ == "__main__":
    print(f"bcplus retrieval server on 127.0.0.1:{PORT} (db: {DB})")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
