"""全ての取得元（設定の配信＋Google ニュース検索＋ウォッチリスト）から記事を集めて保存する。"""
from __future__ import annotations

import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from . import classify, feeds
from .config import Paths, load_sources, load_watchlist
from .store import RETENTION_DAYS, Store, item_id
from .stocks import build_matchers, match_watch

GN_BASE = "https://news.google.com/rss/search"
DEFAULT_LIMIT = 80
_CJK = re.compile(r"[぀-ヿ㐀-鿿]")


def gn_url(query: str, days: int, lang: str) -> str:
    if lang == "ja":
        hl, gl, ceid = "ja", "JP", "JP:ja"
    else:
        hl, gl, ceid = "en-US", "US", "US:en"
    return GN_BASE + "?" + urllib.parse.urlencode({"q": f"{query} when:{days}d", "hl": hl, "gl": gl, "ceid": ceid})


def watch_sources(watchlist: list[dict]) -> list[dict]:
    """ウォッチリストの 1 件につき Google ニュースの検索を 1 本作る。"""
    out = []
    for w in watchlist:
        terms = [w["name"]] + list(w.get("aliases", []))
        code = w.get("code") or ""
        # 数字だけの銘柄コードは検索に使わない（「1911」が拳銃や球団の創設年に当たるなど、無関係な記事が大量に来る）
        if code and re.fullmatch(r"[A-Za-z]{3,}", code):
            terms.append(code)
        out.append({
            "id": "watch:" + w["name"],
            "name": "ウォッチ: " + w["name"],
            "gn": " OR ".join('"%s"' % t for t in terms),
            "days": 7,
            "cat": "biz",
            "lang": "ja" if _CJK.search(w["name"]) else "en",
            "limit": 25,
            "watch": w["name"],
        })
    return out


def _strip_publisher(title: str, publisher: str) -> str:
    """Google ニュースの見出しは末尾に「 - 媒体名」が付くので取り除く。"""
    if publisher:
        for sep in (" - ", " – ", " | "):
            suffix = sep + publisher
            if title.endswith(suffix):
                return title[: -len(suffix)].rstrip()
    return title


def fetch_source(src: dict, fetcher: Callable[[str], bytes], now: float, matchers) -> list[dict]:
    url = src.get("url") or gn_url(src["gn"], int(src.get("days", 2)), src["lang"])
    raw = feeds.parse_feed(fetcher(url))
    raw.sort(key=lambda r: r["published"] or now, reverse=True)
    raw = raw[: int(src.get("limit", DEFAULT_LIMIT))]
    oldest = now - RETENTION_DAYS * 86400
    out: dict[str, dict] = {}
    for r in raw:
        title = r["title"]
        publisher = r["source"]
        source_name = publisher or src["name"]
        if publisher:
            title = _strip_publisher(title, publisher)
        source_name, title = classify.split_trailing_publisher(source_name, title)
        if classify.is_noise(title):
            continue
        company = None
        if src.get("kind") == "tdnet":
            company, title = classify.split_company(title)
            if not (classify.is_important_disclosure(title) or match_watch(matchers, company or "", title)):
                continue
        published = r["published"] if r["published"] else now
        if published > now + 3600:
            published = now
        if published < oldest:
            continue
        link = feeds.normalize_link(r["link"])
        iid = item_id(link)
        if iid in out:
            continue
        out[iid] = {
            "id": iid,
            "title": title,
            "link": link,
            "summary": r["summary"],
            "source": source_name,
            "src_id": src["id"],
            "cat": classify.classify(src["cat"], title, r["summary"], src["lang"]),
            "lang": src["lang"],
            "published": published,
            "first_seen": now,
            "company": company,
            "via": [src["watch"]] if src.get("watch") else [],
        }
    return list(out.values())


def _friendly_error(e: Exception) -> str:
    if isinstance(e, feeds.FeedError):
        return str(e)[:120]
    return f"{type(e).__name__}: {e}"[:120]


def run_refresh(
    paths: Paths,
    *,
    fetcher: Callable[[str], bytes] = feeds.http_get,
    now: float | None = None,
    workers: int = 8,
    gn_pause: float = 0.6,
    progress: Callable[[int, int, str], None] | None = None,
) -> dict:
    now = time.time() if now is None else now
    store = Store(paths.db)
    sources = load_sources(paths)
    watchlist = load_watchlist(paths)
    matchers = build_matchers(watchlist)
    wsrc = watch_sources(watchlist)
    store.drop_missing_sources({s["id"] for s in sources} | {w["id"] for w in wsrc})

    all_sources = sources + wsrc
    total = len(all_sources)
    done = 0
    done_lock = threading.Lock()
    results: dict[str, dict] = {}

    def work(src: dict) -> None:
        nonlocal done
        t0 = time.time()
        try:
            items = fetch_source(src, fetcher, now, matchers)
            new = store.upsert_items(items)
            res = {"ok": True, "count": len(items), "new": new, "err": ""}
        except Exception as e:  # 1 つの取得元の失敗で全体を止めない
            res = {"ok": False, "count": 0, "new": 0, "err": _friendly_error(e)}
        ms = int((time.time() - t0) * 1000)
        store.set_status(src["id"], src["name"], ts=int(now), ok=res["ok"], count=res["count"], new=res["new"],
                         err=res["err"], ms=ms)
        with done_lock:
            results[src["id"]] = res
            done += 1
            if progress:
                progress(done, total, src["name"])

    gn = [s for s in all_sources if s.get("gn")]
    plain = [s for s in all_sources if not s.get("gn")]

    def run_gn() -> None:  # 同じ相手へ続けて送らないよう、間隔を空けて 1 本ずつ
        for i, s in enumerate(gn):
            if i:
                time.sleep(gn_pause)
            work(s)

    t_start = time.time()
    gn_thread = threading.Thread(target=run_gn, daemon=True)
    gn_thread.start()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, plain))
    gn_thread.join()

    purged = store.purge(now)
    ok_ids = [i for i, r in results.items() if r["ok"]]
    failed = {i: r["err"] for i, r in results.items() if not r["ok"]}
    store.set_meta("last_attempt", str(int(now)))
    if ok_ids:
        store.set_meta("last_refresh", str(int(now)))
    return {
        "sources": total,
        "ok": len(ok_ids),
        "failed": failed,
        "new": sum(r["new"] for r in results.values()),
        "purged": purged,
        "seconds": round(time.time() - t_start, 1),
    }
