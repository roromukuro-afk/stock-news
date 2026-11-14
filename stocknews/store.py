"""記事と取得状況の保存（SQLite）。フィードに載る件数は限られるので、見えなくなった記事もここに残す。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

RETENTION_DAYS = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS items(
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  link TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL,
  src_id TEXT NOT NULL,
  cat TEXT NOT NULL,
  lang TEXT NOT NULL,
  published INTEGER NOT NULL,
  first_seen INTEGER NOT NULL,
  company TEXT,
  via TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS ix_items_published ON items(published);
CREATE TABLE IF NOT EXISTS source_status(
  src_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  ts INTEGER NOT NULL,
  ok INTEGER NOT NULL,
  count INTEGER NOT NULL DEFAULT 0,
  new INTEGER NOT NULL DEFAULT 0,
  err TEXT NOT NULL DEFAULT '',
  ms INTEGER NOT NULL DEFAULT 0,
  last_ok INTEGER
);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


def item_id(link: str) -> str:
    return hashlib.sha1(link.encode("utf-8")).hexdigest()[:16]


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        c = sqlite3.connect(str(self.path), timeout=30)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # ---- 書き込み ----
    def upsert_items(self, items: list[dict]) -> int:
        """新しく入った件数を返す。すでにある記事は、ウォッチ由来の印だけ足す。"""
        new = 0
        with self._lock, self._conn() as c:
            for it in items:
                row = c.execute("SELECT via FROM items WHERE id=?", (it["id"],)).fetchone()
                if row is None:
                    c.execute(
                        "INSERT INTO items(id,title,link,summary,source,src_id,cat,lang,published,first_seen,company,via)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (it["id"], it["title"], it["link"], it["summary"], it["source"], it["src_id"], it["cat"],
                         it["lang"], int(it["published"]), int(it["first_seen"]), it.get("company"),
                         json.dumps(it.get("via", []), ensure_ascii=False)),
                    )
                    new += 1
                elif it.get("via"):
                    have = json.loads(row["via"])
                    merged = have + [v for v in it["via"] if v not in have]
                    if merged != have:
                        c.execute("UPDATE items SET via=? WHERE id=?", (json.dumps(merged, ensure_ascii=False), it["id"]))
        return new

    def set_status(self, src_id: str, name: str, *, ts: int, ok: bool, count: int, new: int, err: str, ms: int) -> None:
        with self._lock, self._conn() as c:
            prev = c.execute("SELECT last_ok FROM source_status WHERE src_id=?", (src_id,)).fetchone()
            last_ok = ts if ok else (prev["last_ok"] if prev else None)
            c.execute(
                "INSERT INTO source_status(src_id,name,ts,ok,count,new,err,ms,last_ok) VALUES(?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(src_id) DO UPDATE SET name=excluded.name, ts=excluded.ts, ok=excluded.ok,"
                " count=excluded.count, new=excluded.new, err=excluded.err, ms=excluded.ms, last_ok=excluded.last_ok",
                (src_id, name, ts, int(ok), count, new, err, ms, last_ok),
            )

    def set_meta(self, k: str, v: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    def purge(self, now: float) -> int:
        cutoff = int(now - RETENTION_DAYS * 86400)
        with self._lock, self._conn() as c:
            cur = c.execute("DELETE FROM items WHERE published < ?", (cutoff,))
            return cur.rowcount

    def drop_missing_sources(self, keep_ids: set[str]) -> None:
        """取得元の設定から消えたものの状態表示を消す（記事は残す）。"""
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT src_id FROM source_status").fetchall()
            for r in rows:
                if r["src_id"] not in keep_ids:
                    c.execute("DELETE FROM source_status WHERE src_id=?", (r["src_id"],))

    # ---- 読み出し ----
    def items_since(self, since: float, limit: int = 6000) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM items WHERE published >= ? ORDER BY published DESC, first_seen DESC LIMIT ?",
                (int(since), limit),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["via"] = json.loads(d["via"])
            out.append(d)
        return out

    def statuses(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM source_status ORDER BY name").fetchall()]

    def get_meta(self, k: str, default: str | None = None) -> str | None:
        with self._conn() as c:
            r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
            return r["v"] if r else default

    def count_items(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM items").fetchone()[0]
