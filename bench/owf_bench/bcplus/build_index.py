"""One-time: BrowseComp-Plus corpus (data/bcplus/corpus_hf, 100,195 docs) -> sqlite FTS5 index.

FTS5's built-in bm25() gives us keyword retrieval with zero non-stdlib runtime deps.
Run with an interpreter that has `datasets` (the corpus is an HF dataset on disk);
the resulting docs.db is consumed by server.py with stdlib only.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CORPUS = ROOT / "data/bcplus/corpus_hf"
DB = ROOT / "data/bcplus/docs.db"


def main() -> None:
    from datasets import load_from_disk

    ds = load_from_disk(str(CORPUS))
    DB.unlink(missing_ok=True)
    conn = sqlite3.connect(DB)
    conn.executescript(
        """
        CREATE TABLE docs (docid TEXT PRIMARY KEY, url TEXT, text TEXT);
        CREATE VIRTUAL TABLE docs_fts USING fts5(text, content='docs', content_rowid='rowid', tokenize='porter unicode61');
        """
    )
    batch = []
    for i, row in enumerate(ds):
        batch.append((row["docid"], row.get("url") or "", row["text"]))
        if len(batch) >= 5000:
            conn.executemany("INSERT INTO docs VALUES (?,?,?)", batch)
            batch = []
            print(f"{i + 1}/{len(ds)}")
    if batch:
        conn.executemany("INSERT INTO docs VALUES (?,?,?)", batch)
    conn.execute("INSERT INTO docs_fts(rowid, text) SELECT rowid, text FROM docs")
    conn.commit()
    conn.execute("INSERT INTO docs_fts(docs_fts) VALUES('optimize')")
    conn.commit()
    conn.close()
    print(f"indexed {len(ds)} docs -> {DB}")


if __name__ == "__main__":
    main()
