"""標準ライブラリの unittest だけで動く。実行: python -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stocknews import classify, config, export, feeds, refresh, server, stocks, textmatch  # noqa: E402
from stocknews.config import Paths  # noqa: E402
from stocknews.store import Store  # noqa: E402

NOW = 1_794_000_000.0  # 固定の「いま」


def rss(*items: str, extra: str = "") -> bytes:
    return ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>t</title>' + extra
            + "".join(items) + "</channel></rss>").encode("utf-8")


def item(title: str, link: str, date: str = "Sat, 14 Nov 2026 09:00:00 +0900", desc: str = "", source: str = "") -> str:
    s = f"<item><title>{title}</title><link>{link}</link><pubDate>{date}</pubDate>"
    if desc:
        s += f"<description>{desc}</description>"
    if source:
        s += f"<source>{source}</source>"
    return s + "</item>"


class FeedParseTests(unittest.TestCase):
    def test_rss_basic_and_entities(self):
        data = rss(item("A &amp; B", "https://e.com/1", desc="&lt;p&gt;本文 &amp;amp; 続き&lt;/p&gt;"))
        [it] = feeds.parse_feed(data)
        self.assertEqual(it["title"], "A & B")
        self.assertEqual(it["summary"], "本文 & 続き")
        self.assertEqual(it["link"], "https://e.com/1")

    def test_atom(self):
        data = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Hello</title>'
                '<link rel="self" href="https://e.com/self"/><link rel="alternate" href="https://e.com/a"/>'
                '<updated>2026-11-14T01:00:00Z</updated><summary>S</summary></entry></feed>').encode()
        [it] = feeds.parse_feed(data)
        self.assertEqual(it["link"], "https://e.com/a")
        self.assertEqual(it["published"], feeds.parse_date("2026-11-14T01:00:00Z"))

    def test_rdf(self):
        data = ('<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/" '
                'xmlns:dc="http://purl.org/dc/elements/1.1/"><item><title>見出し</title><link>https://e.com/r</link>'
                '<dc:date>2026-11-14T16:14:21+09:00</dc:date></item></rdf:RDF>').encode()
        [it] = feeds.parse_feed(data)
        self.assertEqual(it["title"], "見出し")
        self.assertEqual(it["published"], feeds.parse_date("2026-11-14T07:14:21Z"))

    def test_lenient_repair(self):
        # 裸の &・HTML の実体参照・制御文字が混ざっていても読める
        raw = ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
               '<item><title>AT&T &nbsp; news\x0b</title><link>https://e.com/x</link></item></channel></rss>').encode()
        [it] = feeds.parse_feed(raw)
        self.assertIn("AT&T", it["title"])

    def test_html_page_is_not_a_feed(self):
        with self.assertRaises(feeds.FeedError) as cm:
            feeds.parse_feed(b"<!DOCTYPE html><html><head><title>x</title><script>if (a && b) {}</script></head></html>")
        self.assertIn("HTML", str(cm.exception))

    def test_items_without_title_or_link_are_skipped(self):
        data = rss("<item><title>only title</title></item>", "<item><link>https://e.com/1</link></item>")
        self.assertEqual(feeds.parse_feed(data), [])

    def test_summary_that_repeats_title_is_dropped_and_long_is_cut(self):
        self.assertEqual(feeds.summary_if_useful("日経平均が反発", "日経平均が反発"), "")
        self.assertEqual(len(feeds.summary_if_useful("t", "あ" * 500)), feeds.SUMMARY_MAX)


class DateAndLinkTests(unittest.TestCase):
    def test_timezones(self):
        base = feeds.parse_date("Fri, 13 Nov 2026 07:00:00 GMT")
        self.assertEqual(feeds.parse_date("Fri, 13 Nov 2026 16:00:00 +0900"), base)
        self.assertEqual(feeds.parse_date("Fri, 13 Nov 2026 16:00:00 JST"), base)   # 日本の官公庁の配信
        self.assertEqual(feeds.parse_date("2026-11-13 07:00:00"), base)              # 時刻帯なしは UTC
        self.assertIsNone(feeds.parse_date("きのう"))
        self.assertIsNone(feeds.parse_date(""))

    def test_normalize_link(self):
        self.assertEqual(feeds.normalize_link("https://e.com/a?utm_source=x&id=3#top"), "https://e.com/a?id=3")
        self.assertEqual(
            feeds.normalize_link("https://webapi.yanoshin.jp/rd.php?https://www.release.tdnet.info/inbs/1.pdf"),
            "https://www.release.tdnet.info/inbs/1.pdf")


class ClassifyTests(unittest.TestCase):
    def test_categories(self):
        c = classify.classify
        self.assertEqual(c("biz", "トヨタ、最高益を更新　上方修正", "", "ja"), "market")
        self.assertEqual(c("biz", "日銀が追加利上げを決定", "", "ja"), "macro")
        self.assertEqual(c("general", "高校ラグビー予選", "", "ja"), "general")
        self.assertEqual(c("biz", "Nvidia shares fall after earnings", "", "en"), "market")
        self.assertEqual(c("biz", "Fed holds interest rates steady", "", "en"), "macro")
        self.assertEqual(c("biz", "Smog hangs over Lahore", "", "en"), "biz")
        self.assertEqual(c("disclosure", "なんでも", "", "ja"), "disclosure")   # 開示・政策そのものの配信は動かさない

    def test_noise_pages_and_real_articles(self):
        self.assertTrue(classify.is_noise("テスラ【TSLA】：株価・株式情報"))
        self.assertTrue(classify.is_noise("NYダウの指数情報・推移"))
        self.assertTrue(classify.is_noise("つみたて新興国株式【03314178】：基準価格・投資信託情報"))
        self.assertFalse(classify.is_noise("日経平均は反発、半導体株に買い戻し"))
        self.assertFalse(classify.is_noise("株価が上がる理由：決算が好調"))

    def test_trailing_publisher_only_for_aggregators(self):
        self.assertEqual(classify.split_trailing_publisher("Yahoo!ファイナンス", "日経平均は反発(株探ニュース)"),
                         ("株探ニュース", "日経平均は反発"))
        self.assertEqual(classify.split_trailing_publisher("Yahoo!ファイナンス", "先物 85円安 （11月13日）"),
                         ("Yahoo!ファイナンス", "先物 85円安 （11月13日）"))
        self.assertEqual(classify.split_trailing_publisher("Reuters", "x(ロイター)"), ("Reuters", "x(ロイター)"))

    def test_disclosure_filter_and_company(self):
        self.assertTrue(classify.is_important_disclosure("通期業績予想の修正に関するお知らせ"))
        self.assertTrue(classify.is_important_disclosure("2026年９月期 決算短信〔ＩＦＲＳ〕（連結）"))
        self.assertFalse(classify.is_important_disclosure("コーポレート・ガバナンスに関する報告書"))
        self.assertEqual(classify.split_company("ニデック:半期報告書の提出期限延長"), ("ニデック", "半期報告書の提出期限延長"))
        self.assertEqual(classify.split_company("会社名なしの件名"), (None, "会社名なしの件名"))


class TextMatchTests(unittest.TestCase):
    def matchers(self):
        return stocks.build_matchers(config.clean_watchlist([
            {"name": "トヨタ自動車", "code": "7203", "aliases": "トヨタ, Toyota"},
            {"name": "エヌビディア", "code": "NVDA"},
            {"name": "フォード", "code": "F"},
        ]))

    def test_name_alias_and_width_insensitive(self):
        m = self.matchers()
        self.assertEqual(stocks.match_watch(m, "ｔｏｙｏｔａ、新工場へ"), ["トヨタ自動車"])
        self.assertEqual(stocks.match_watch(m, "トヨタが最高益"), ["トヨタ自動車"])

    def test_code_needs_word_boundary(self):
        m = self.matchers()
        self.assertEqual(stocks.match_watch(m, "銘柄コード（7203）の決算"), ["トヨタ自動車"])
        self.assertEqual(stocks.match_watch(m, "指数は172030円"), [])          # 数字の一部は別物
        self.assertEqual(stocks.match_watch(m, "NVDA rises"), ["エヌビディア"])
        self.assertEqual(stocks.match_watch(m, "NVDAX is different"), [])
        self.assertEqual(stocks.match_watch(m, "F rises, a fine day"), [])      # 1〜2 文字のティッカーは照合に使わない

    def mk(self, title, lang="ja", t=NOW, company=None, src="a"):
        return {"title": title, "lang": lang, "published": t, "company": company, "source": src}

    def test_cluster_merges_same_story_across_sources(self):
        items = [self.mk("3メガバンク中間決算 最高益更新"), self.mk("3メガバンクG 中間決算で過去最高益", t=NOW - 60),
                 self.mk("全く別の話題の見出しです")]
        groups = textmatch.cluster_items(items)
        self.assertEqual([len(g) for g in groups], [2, 1])

    def test_cluster_does_not_merge_templated_series_or_companies(self):
        a = self.mk("週間ランキング【値上がり率】 (11月13日)")
        b = self.mk("週間ランキング【値下がり率】 (11月13日)", t=NOW - 60)
        self.assertEqual(len(textmatch.cluster_items([a, b])), 2)
        a = self.mk("１３日の為替市場の四本値（ドル円・ユーロドル・ユーロ円）")
        b = self.mk("１２日の為替市場の四本値（ドル円・ユーロドル・ユーロ円）", t=NOW - 60)
        self.assertEqual(len(textmatch.cluster_items([a, b])), 2)
        a = self.mk("決算短信〔日本基準〕（連結）", company="甲社")
        b = self.mk("決算短信〔日本基準〕（連結）", company="乙社", t=NOW - 60)
        self.assertEqual(len(textmatch.cluster_items([a, b])), 2)

    def test_cluster_respects_language_and_time_window(self):
        a = self.mk("Trump will not raise Nvidia spy issue with Xi at summit", "en")
        b = self.mk("Trump will not raise Nvidia spy issue with Xi at summit", "ja", t=NOW - 60)
        self.assertEqual(len(textmatch.cluster_items([a, b])), 2)
        c = self.mk("Trump will not raise Nvidia spy issue with Xi at summit", "en", t=NOW - 3 * 86400)
        self.assertEqual(len(textmatch.cluster_items([a, c])), 2)


class ConfigTests(unittest.TestCase):
    def test_clean_watch_validation(self):
        w = config.clean_watch({"name": " トヨタ ", "code": "7203", "aliases": "Toyota、トヨタ自"})
        self.assertEqual(w, {"name": "トヨタ", "code": "7203", "aliases": ["Toyota", "トヨタ自"], "exclude": []})
        for bad in ({"name": "a"}, {"name": "x" * 41}, {"name": "ok名前", "code": "a b"}, {"name": "ok名前", "aliases": ["z"]}):
            with self.assertRaises(ValueError):
                config.clean_watch(bad)

    def test_clean_watchlist_dedupes_and_limits(self):
        out = config.clean_watchlist([{"name": "Nvidia"}, {"name": "NVIDIA"}])
        self.assertEqual(len(out), 1)
        with self.assertRaises(ValueError):
            config.clean_watchlist([{"name": f"銘柄{i:03d}"} for i in range(config.MAX_WATCH + 1)])
        with self.assertRaises(ValueError):
            config.clean_watchlist("not a list")

    def test_shipped_sources_are_valid(self):
        sources = config.load_sources(Paths())
        self.assertGreater(len(sources), 30)
        self.assertTrue(all(s.get("url") or s.get("gn") for s in sources))


def make_root(sources: list[dict], watchlist: list[dict] | None = None) -> Paths:
    root = Path(tempfile.mkdtemp(prefix="stocknews_test_"))
    (root / "config").mkdir()
    (root / "site").mkdir()
    (root / "site" / "index.html").write_text("<!doctype html><title>t</title>", encoding="utf-8")
    (root / "config" / "sources.json").write_text(json.dumps({"sources": sources}, ensure_ascii=False), encoding="utf-8")
    (root / "config" / "watchlist.json").write_text(json.dumps(watchlist or []), encoding="utf-8")
    return Paths(root)


def src(sid, url, cat="biz", lang="ja", **kw):
    return {"id": sid, "name": sid.upper(), "url": url, "cat": cat, "lang": lang, **kw}


class RefreshTests(unittest.TestCase):
    def test_pipeline_filters_dedupes_and_survives_failures(self):
        now = feeds.parse_date("Sat, 14 Nov 2026 10:00:00 +0900")
        paths = make_root(
            [src("a", "http://a/feed"), src("b", "http://b/feed"), src("bad", "http://bad/feed"),
             src("td", "http://td/feed", "disclosure", kind="tdnet")],
            watchlist=[{"name": "丙社", "code": "", "aliases": []}],
        )
        feeds_by_url = {
            "http://a/feed": rss(
                item("日経平均が大幅反発", "http://x/1?utm_source=z"),
                item("テスラ【TSLA】：株価・株式情報", "http://x/page"),
                item("古い記事", "http://x/old", date="Mon, 01 Jan 2024 00:00:00 +0900"),
                item("未来の日付", "http://x/future", date="Mon, 01 Jan 2040 00:00:00 +0900"),
            ),
            "http://b/feed": rss(item("日経平均が大幅反発", "http://x/1")),
            "http://td/feed": rss(
                item("甲社:通期業績予想の修正に関するお知らせ", "https://webapi.yanoshin.jp/rd.php?http://t/1.pdf"),
                item("乙社:コーポレート・ガバナンスに関する報告書", "https://webapi.yanoshin.jp/rd.php?http://t/2.pdf"),
                item("丙社:コーポレート・ガバナンスに関する報告書", "https://webapi.yanoshin.jp/rd.php?http://t/3.pdf"),
            ),
        }

        def fetcher(url):
            if url == "http://bad/feed":
                raise feeds.FeedError("HTTP 404")
            if url.startswith("https://news.google.com/"):
                return rss()
            return feeds_by_url[url]

        res = refresh.run_refresh(paths, fetcher=fetcher, now=now, gn_pause=0)
        self.assertEqual(res["failed"], {"bad": "HTTP 404"})
        store = Store(paths.db)
        items = store.items_since(0)
        titles = sorted(i["title"] for i in items)
        self.assertEqual(titles, sorted(["日経平均が大幅反発", "未来の日付", "通期業績予想の修正に関するお知らせ",
                                         "コーポレート・ガバナンスに関する報告書"]))
        links = {i["link"] for i in items}
        self.assertIn("http://x/1", links)                       # 追跡用の付録を外し、同じ記事は 1 件
        self.assertIn("http://t/1.pdf", links)                   # TDnet の中継 URL は本来の URL に
        future = next(i for i in items if i["title"] == "未来の日付")
        self.assertLessEqual(future["published"], now + 3600)    # 未来の日付は「いま」に丸める
        td_watch = next(i for i in items if i["company"] == "丙社")
        self.assertEqual(td_watch["cat"], "disclosure")           # 重要でなくてもウォッチ中の会社の開示は残す
        st = {s["src_id"]: s for s in store.statuses()}
        self.assertFalse(st["bad"]["ok"])
        self.assertEqual(st["bad"]["err"], "HTTP 404")
        self.assertTrue(st["a"]["ok"])
        self.assertEqual(store.get_meta("last_refresh"), str(int(now)))

    def test_watch_sources_and_via_merge(self):
        now = feeds.parse_date("Sat, 14 Nov 2026 10:00:00 +0900")
        paths = make_root([src("a", "http://a/feed")], watchlist=[{"name": "エヌビディア", "code": "NVDA", "aliases": ["Nvidia"]}])
        urls = []

        def fetcher(url):
            urls.append(url)
            if url.startswith("https://news.google.com/"):
                return rss(item("エヌビディア決算 - 日経", "http://n/1", source="日経"))
            return rss(item("エヌビディア決算", "http://n/1"))

        refresh.run_refresh(paths, fetcher=fetcher, now=now, gn_pause=0)
        gn = [u for u in urls if "news.google.com" in u]
        self.assertEqual(len(gn), 1)
        self.assertIn("%22%E3%82%A8%E3%83%8C%E3%83%93%E3%83%87%E3%82%A3%E3%82%A2%22", gn[0])   # "エヌビディア" を引用符つきで検索
        self.assertIn("NVDA", gn[0])
        [it] = Store(paths.db).items_since(0)
        self.assertEqual(it["via"], ["エヌビディア"])           # 先に別の配信で入っていた記事にも、ウォッチ由来の印が足される

    def test_everything_failing_keeps_last_refresh_unset(self):
        paths = make_root([src("a", "http://a/feed")])

        def fetcher(url):
            raise OSError("offline")

        res = refresh.run_refresh(paths, fetcher=fetcher, now=NOW, gn_pause=0)
        self.assertEqual(res["ok"], 0)
        store = Store(paths.db)
        self.assertIsNone(store.get_meta("last_refresh"))
        self.assertEqual(store.get_meta("last_attempt"), str(int(NOW)))

    def test_purge_old_items(self):
        paths = make_root([src("a", "http://a/feed")])
        store = Store(paths.db)
        row = lambda i, pub: {"id": i, "title": i, "link": "http://" + i, "summary": "", "source": "s", "src_id": "a",
                              "cat": "biz", "lang": "ja", "published": pub, "first_seen": pub}
        store.upsert_items([row("old", NOW - 40 * 86400), row("new", NOW - 86400)])
        self.assertEqual(store.purge(NOW), 1)
        self.assertEqual([i["id"] for i in store.items_since(0)], ["new"])


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.paths = make_root([src("a", "http://a/feed", "biz")])
        self.store = Store(self.paths.db)
        row = lambda i, title, **kw: {"id": i, "title": title, "link": "http://x/" + i, "summary": "", "source": "S",
                                      "src_id": "a", "cat": "biz", "lang": "ja", "published": NOW - 100, "first_seen": NOW,
                                      **kw}
        self.store.upsert_items([
            row("1", "トヨタが最高益を更新", via=["削除ずみの銘柄"]),
            row("2", "高校ラグビー予選"),
            row("3", "ノイズ【ABC】：株価・株式情報"),
        ])

    def test_category_is_reclassified_at_export(self):
        p = export.build_payload(self.store, [], NOW, default_cats={"a": "biz"})
        cats = {i["title"]: i["cat"] for i in p["items"]}
        self.assertEqual(cats["トヨタが最高益を更新"], "market")     # 取り込み時は biz だったが、いまの規則で判定し直す
        self.assertEqual(cats["高校ラグビー予選"], "biz")
        self.assertNotIn("ノイズ【ABC】：株価・株式情報", cats)       # ノイズは保存済みでも出さない

    def test_stale_watch_marker_is_not_shown_after_removal(self):
        p = export.build_payload(self.store, [], NOW)
        self.assertTrue(all(i["stocks"] == [] for i in p["items"]))
        p = export.build_payload(self.store, [{"name": "トヨタ", "code": "", "aliases": [], "exclude": []}], NOW)
        self.assertEqual({i["title"]: i["stocks"] for i in p["items"]}["トヨタが最高益を更新"], [{"n": "トヨタ", "r": "certain"}])

    def test_write_export_is_valid_json(self):
        payload = export.write_export(self.paths, NOW)
        on_disk = json.loads(self.paths.news_json.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["generated"], payload["generated"])
        self.assertEqual(len(on_disk["items"]), 2)


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.paths = make_root([src("a", "http://a/feed")])
        cls.calls = 0
        gate = cls.gate = threading.Event()

        def fake_refresh(paths, progress=None, **kw):
            cls.calls += 1
            gate.wait(5)
            return {}

        cls.app = server.App(cls.paths, 0, run_refresh_fn=fake_refresh)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(cls.app))
        cls.port = cls.httpd.server_address[1]
        cls.app.allowed_hosts = {f"127.0.0.1:{cls.port}", f"localhost:{cls.port}"}
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.gate.set()
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def req(self, method, path, body=None, headers=None, host=None):
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        h.update(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    CSRF = {"X-Requested-With": "stocknews"}

    def test_static_page_and_security_headers(self):
        status, body, headers = self.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_unknown_host_is_rejected(self):
        status, _, _ = self.req("GET", "/api/status", host="evil.example:80")
        self.assertEqual(status, 403)

    def test_post_requires_header_and_same_origin(self):
        self.assertEqual(self.req("POST", "/api/refresh")[0], 403)
        self.assertEqual(self.req("POST", "/api/refresh", headers={**self.CSRF, "Origin": "http://evil.example"})[0], 403)

    def test_path_traversal_does_not_leak_files(self):
        for p in ("/../config/sources.json", "/..%2fconfig/sources.json", "/%2e%2e/config/sources.json"):
            status, body, _ = self.req("GET", p)
            self.assertNotEqual(status, 200, p)

    def test_refresh_runs_once_at_a_time(self):
        self.gate.clear()
        before = type(self).calls
        s1, b1, _ = self.req("POST", "/api/refresh", headers=self.CSRF)
        time.sleep(0.2)
        s2, b2, _ = self.req("POST", "/api/refresh", headers=self.CSRF)
        self.assertTrue(json.loads(b1)["started"])
        self.assertFalse(json.loads(b2)["started"])            # 実行中は重ねて始めない
        self.assertTrue(json.loads(self.req("GET", "/api/status")[1])["updating"])
        self.gate.set()
        for _ in range(50):
            if not self.app.state["updating"]:
                break
            time.sleep(0.1)
        self.assertEqual(type(self).calls - before, 1)

    def test_watchlist_save_and_validation(self):
        self.gate.set()
        for _ in range(50):                                      # 前の取り直しが終わるまで待つ
            if not self.app.state["updating"]:
                break
            time.sleep(0.1)
        s, b, _ = self.req("POST", "/api/watchlist", {"watchlist": [{"name": "トヨタ自動車", "code": "7203"}]}, self.CSRF)
        self.assertEqual(s, 200)
        self.assertEqual(json.loads(b)["watchlist"][0]["name"], "トヨタ自動車")
        self.assertEqual(json.loads(self.req("GET", "/api/watchlist")[1])["watchlist"][0]["code"], "7203")
        self.assertTrue(self.paths.news_json.exists())            # 保存と同時に書き出しも更新される
        s, b, _ = self.req("POST", "/api/watchlist", {"watchlist": [{"name": "x"}]}, self.CSRF)
        self.assertEqual(s, 400)
        self.assertIn("名前", json.loads(b)["error"])
        s, _, _ = self.req("POST", "/api/watchlist", {"watchlist": "oops"}, self.CSRF)
        self.assertEqual(s, 400)
        # 壊れた JSON でも落ちずに 400
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/watchlist", data=b"{not json", method="POST",
                                   headers={"Host": f"127.0.0.1:{self.port}", "Content-Type": "application/json", **self.CSRF})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(r, timeout=5)
        self.assertEqual(cm.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
