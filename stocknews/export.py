"""保存した記事を、画面が読む 1 つの JSON（site/data/news.json）に書き出す。

カテゴリ・銘柄ごとの分類・種類・重要度は、書き出しのたびに判定し直す（規則や除外語を直したとき、取り直さずに反映できる）。
"""
from __future__ import annotations

import json
import time

from . import stocks as st
from .classify import classify, is_noise
from .config import Paths, atomic_write, load_sources, load_watchlist
from .store import Store
from .textmatch import cluster_items

EXPORT_DAYS = 7
MAX_GROUPS = 2500
MAX_EXCLUDED_PER_STOCK = 40
_REL_RANK = {st.CERTAIN: 2, st.UNCERTAIN: 1}


def build_payload(store: Store, watchlist: list[dict], now: float, days: int = EXPORT_DAYS,
                  default_cats: dict[str, str] | None = None) -> dict:
    matchers = st.build_matchers(watchlist)
    names = {w["name"] for w in watchlist}
    default_cats = default_cats or {}
    items = [it for it in store.items_since(now - days * 86400) if not is_noise(it["title"])]
    excluded: dict[str, list[dict]] = {m.name: [] for m in matchers}
    for it in items:
        default = default_cats.get(it["src_id"], "biz" if it["src_id"].startswith("watch:") else it["cat"])
        it["cat"] = classify(default, it["title"], it["summary"], it["lang"])
        safe = it["cat"] == "disclosure"            # 公式の開示（TDnet）は除外しない
        via = {v for v in it["via"] if v in names}  # 取得時の「その銘柄の検索で当たった」印は、いまの登録名だけ使う
        it["stocks"] = {}
        for m in matchers:
            rel, reason = st.assess(m, it["title"], it["summary"], it.get("company"), via=m.name in via, safe=safe)
            if rel in _REL_RANK:
                it["stocks"][m.name] = rel
            elif rel == st.EXCLUDED:
                excluded[m.name].append({"id": it["id"], "title": it["title"], "url": it["link"], "source": it["source"],
                                         "time": it["published"], "reason": reason})
    groups = cluster_items(items)[:MAX_GROUPS]
    out_items = []
    for g in groups:
        lead = g[0]
        stocks_rel: dict[str, str] = {}
        for m in g:                                   # 同じ話題のどれかが関連していれば、その銘柄の話題
            for name, rel in m["stocks"].items():
                if _REL_RANK[rel] > _REL_RANK.get(stocks_rel.get(name, ""), 0):
                    stocks_rel[name] = rel
        entry = {
            "id": lead["id"],
            "title": lead["title"],
            "url": lead["link"],
            "summary": lead["summary"],
            "source": lead["source"],
            "cat": lead["cat"],
            "lang": lead["lang"],
            "time": lead["published"],
            "company": lead.get("company"),
            "stocks": [{"n": n, "r": r} for n, r in stocks_rel.items()],
            "more": [{"source": m["source"], "title": m["title"], "url": m["link"], "time": m["published"]} for m in g[1:8]],
            "more_count": max(0, len(g) - 1),
        }
        if stocks_rel or lead["cat"] == "disclosure":
            entry["kind"] = st.event_kind(lead["title"], lead["summary"])
            tier, reason = st.importance(lead["title"], lead["summary"], safe=lead["cat"] == "disclosure")
            entry["tier"] = tier
            entry["tier_reason"] = reason
        out_items.append(entry)
    for name in excluded:                             # 除外した記事は、同じ話題の重複を省いて新しい順に
        seen: set[str] = set()
        uniq = []
        for e in sorted(excluded[name], key=lambda e: -e["time"]):
            if e["title"] not in seen:
                seen.add(e["title"])
                uniq.append(e)
        excluded[name] = uniq[:MAX_EXCLUDED_PER_STOCK]
    statuses = [
        {"id": s["src_id"], "name": s["name"], "ok": bool(s["ok"]), "count": s["count"], "new": s["new"],
         "error": s["err"], "time": s["ts"], "last_ok": s["last_ok"]}
        for s in store.statuses()
    ]
    last_refresh = store.get_meta("last_refresh")
    return {
        "generated": int(now),
        "last_refresh": int(last_refresh) if last_refresh else None,
        "items": out_items,
        "sources": statuses,
        "watchlist": watchlist,
        "excluded": {k: v for k, v in excluded.items() if v},
        "kinds": st.KIND_LABELS,
        "tiers": st.TIER_LABELS,
    }


def write_export(paths: Paths, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    default_cats = {s["id"]: s["cat"] for s in load_sources(paths)}
    payload = build_payload(Store(paths.db), load_watchlist(paths), now, default_cats=default_cats)
    atomic_write(paths.news_json, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return payload
