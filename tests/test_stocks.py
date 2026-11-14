"""銘柄ごとの記事分類（stocknews/stocks.py と、書き出しでの使い方）の試験。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stocknews import config, export, stocks as st  # noqa: E402
from stocknews.config import Paths  # noqa: E402
from stocknews.store import Store  # noqa: E402

NOW = 1_794_000_000.0

WATCH = config.clean_watchlist([
    {"name": "ソフトバンク", "code": "9434", "exclude": "ホークス, プロ野球, 登板"},
    {"name": "ソフトバンクグループ", "code": "9984", "aliases": "ソフトバンクG", "exclude": "ホークス"},
    {"name": "オリックス", "code": "8591", "aliases": "ORIX", "exclude": "バファローズ, プロ野球, 先発"},
    {"name": "JT", "code": "2914", "aliases": "日本たばこ産業", "exclude": "JT杯, 将棋"},
    {"name": "住友林業", "code": "1911"},
    {"name": "エヌビディア", "code": "NVDA", "aliases": "Nvidia"},
    {"name": "東宝", "code": "9602"},
])
M = {m.name: m for m in st.build_matchers(WATCH)}


def a(name, title, summary="", company=None, via=False, safe=False):
    return st.assess(M[name], title, summary, company, via=via, safe=safe)[0]


class RelevanceTests(unittest.TestCase):
    def test_certain_when_name_in_title_uncertain_when_only_in_summary(self):
        self.assertEqual(a("東宝", "東宝、映画の興行収入が好調"), "certain")
        self.assertEqual(a("東宝", "映画の興行収入が好調", "配給の東宝は…"), "uncertain")
        self.assertIsNone(a("東宝", "映画の興行収入が好調", "配給会社は…"))

    def test_search_hit_without_name_is_uncertain_but_not_for_other_stocks(self):
        self.assertEqual(a("東宝", "ゴジラの新作", via=True), "uncertain")
        self.assertIsNone(a("住友林業", "ゴジラの新作", via=False))

    def test_longer_name_of_another_stock_is_not_counted_for_the_shorter(self):
        self.assertIsNone(a("ソフトバンク", "ソフトバンクグループが最高益"))
        self.assertEqual(a("ソフトバンクグループ", "ソフトバンクグループが最高益"), "certain")
        self.assertEqual(a("ソフトバンクグループ", "ソフトバンクGが投資を発表"), "certain")
        # 両方に触れている記事は、両方の銘柄の記事
        self.assertEqual(a("ソフトバンク", "ソフトバンクグループとソフトバンクが提携"), "certain")

    def test_ascii_terms_need_word_boundaries(self):
        self.assertIsNone(a("JT", "JTB、旅行商品を発売"))
        self.assertEqual(a("JT", "JTが決算を発表"), "certain")
        self.assertEqual(a("JT", "日本たばこ産業が増配"), "certain")

    def test_code_only_counts_in_stock_code_forms(self):
        self.assertEqual(a("住友林業", "木造ビル需要が拡大", "（1911）の株価"), "uncertain")
        self.assertEqual(a("住友林業", "木造ビル需要が拡大", "証券コード 1911"), "uncertain")
        self.assertEqual(a("住友林業", "ビル需要", "1911.T が上昇"), "uncertain")
        # 西暦・型番・創設年の「1911」には当たらない
        for text in ("Springfield Armory 1911 review", "1911年創業のクラブ", "Colt 1911 pistol", "1911"):
            self.assertIsNone(a("住友林業", text, text), text)

    def test_us_ticker_needs_three_letters_and_boundaries(self):
        self.assertEqual(a("エヌビディア", "NVDA rises after earnings"), "uncertain")   # コードだけ。社名は無い
        self.assertIsNone(a("エヌビディア", "NVDAX is a different thing"))
        self.assertEqual(a("エヌビディア", "Nvidia shares fall"), "certain")


class NoiseTests(unittest.TestCase):
    def test_exclude_words_drop_the_article(self):
        rel, reason = st.assess(M["オリックス"], "オリックス・バファローズが連勝", "", None, via=False, safe=False)
        self.assertEqual(rel, st.EXCLUDED)
        self.assertIn("バファローズ", reason)
        self.assertEqual(a("ソフトバンク", "福岡ソフトバンクホークスの試合結果"), st.EXCLUDED)

    def test_protect_keyword_keeps_the_article_as_uncertain(self):
        rel, reason = st.assess(M["オリックス"], "オリックス、買収を発表 バファローズ売却は否定", "", None, via=False, safe=False)
        self.assertEqual(rel, st.UNCERTAIN)
        self.assertIn("保護語", reason)

    def test_protect_words_are_compound_not_bare(self):
        # 「決算」「契約」だけでは保護しない（大決算セール・契約更改を保護してしまうため）
        self.assertIsNone(st.protected_by(st.fold("大決算セール 契約更改")))
        self.assertEqual(st.protected_by(st.fold("決算短信を公表")), "決算短信")
        self.assertEqual(st.protected_by(st.fold("IR情報を更新")), "IR情報")
        self.assertIsNone(st.protected_by(st.fold("a firm decision on first aid")))   # "IR" の部分一致で保護しない

    def test_official_disclosure_is_never_excluded(self):
        rel, _ = st.assess(M["オリックス"], "先発投手の件に関するお知らせ", "", "オリックス", via=False, safe=True)
        self.assertEqual(rel, st.CERTAIN)

    def test_market_report_spam_is_excluded_for_every_stock(self):
        rel, reason = st.assess(M["東宝"], "東宝 市場規模は2032年までに拡大 CAGR 8%", "", None, via=False, safe=False)
        self.assertEqual(rel, st.EXCLUDED)
        self.assertIn("市場調査", reason)


class KindAndTierTests(unittest.TestCase):
    def test_event_kind(self):
        k = st.event_kind
        self.assertEqual(k("2026年９月期 決算短信〔ＩＦＲＳ〕（連結）"), "earnings")
        self.assertEqual(k("通期業績予想の上方修正に関するお知らせ"), "guidance")
        self.assertEqual(k("剰余金の配当（増配）に関するお知らせ"), "dividend")
        self.assertEqual(k("自己株式の取得に係る事項の決定"), "buyback")
        self.assertEqual(k("株式会社Wisdomによる公開買付けの開始"), "ma")
        self.assertEqual(k("資本業務提携に関するお知らせ"), "alliance")
        self.assertEqual(k("代表取締役の異動に関するお知らせ"), "personnel")
        self.assertEqual(k("第100回定時株主総会の招集"), "agm")
        self.assertEqual(k("行政処分を受けました"), "legal")
        self.assertEqual(k("新製品を発売"), "product")
        self.assertEqual(k("本社ビルの清掃について"), "other")
        self.assertEqual(k("決算マイナス・インパクト銘柄 【東証プライム】"), "earnings")
        self.assertEqual(k("業績修正（取引時間中）－甲ＦＧ、乙工業"), "guidance")
        self.assertEqual(k("年末の大決算セール開催"), "other")     # 「大決算セール」は決算ではない

    def test_importance(self):
        imp = st.importance
        self.assertEqual(imp("通期業績予想の下方修正")[0], "critical")
        self.assertEqual(imp("無配に転落")[0], "critical")
        self.assertEqual(imp("決算短信")[0], "important")
        self.assertEqual(imp("新工場の稼働")[0], "important")
        self.assertEqual(imp("コーポレートガバナンス報告書の更新")[0], "normal")
        self.assertEqual(imp("コーポレートガバナンス報告書の更新", safe=True), ("important", "公式開示"))   # 公式開示は既定で「重要」


class ClusterWithStocksTests(unittest.TestCase):
    def mk(self, title, stocks, t=NOW):
        return {"title": title, "lang": "ja", "published": t, "company": None, "stocks": stocks}

    def test_similar_titles_about_different_stocks_are_not_merged(self):
        from stocknews import textmatch
        a = self.mk("【決算速報】甲FG、今期最終を上方修正・最高益予想を上乗せ、配当も増額", {"甲銀行": "certain"})
        b = self.mk("【決算速報】乙ＵＦＪ、今期最終を上方修正・最高益予想を上乗せ、配当も増額", {}, t=NOW - 60)
        self.assertEqual(len(textmatch.cluster_items([a, b])), 2)       # 片方にしか銘柄が付かない
        c = self.mk("【決算速報】甲FG、今期最終を上方修正・最高益予想を上乗せ、配当も増額", {"甲銀行": "certain"}, t=NOW - 120)
        self.assertEqual([len(g) for g in textmatch.cluster_items([a, c])], [2])   # 同じ銘柄の同じ話題は束ねる


class ConfigExcludeTests(unittest.TestCase):
    def test_exclude_is_cleaned_and_limited(self):
        w = config.clean_watch({"name": "オリックス", "exclude": "野球、バファローズ, 野球"})
        self.assertEqual(w["exclude"], ["野球", "バファローズ"])
        with self.assertRaises(ValueError):
            config.clean_watch({"name": "オリックス", "exclude": ["x"]})
        w = config.clean_watch({"name": "オリックス", "exclude": [f"語{i:02d}" for i in range(50)]})
        self.assertEqual(len(w["exclude"]), config.MAX_EXCLUDE)

    @unittest.skipUnless(Paths().watchlist.exists(), "銘柄リストは公開リポジトリに置かない（秘密の変数で渡す）")
    def test_shipped_watchlist_loads_and_is_consistent(self):
        wl = config.load_watchlist(Paths())
        names = [w["name"] for w in wl]
        self.assertEqual(len(names), len(set(names)))
        self.assertGreaterEqual(len(wl), 1)       # 銘柄リストは公開リポジトリに置かない。手元にあるときだけ、中身の形を確かめる
        for w in wl:
            self.assertTrue(w["name"])
            self.assertIsInstance(w["exclude"], list)


class ExportIntegrationTests(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp(prefix="stocknews_stocks_"))
        self.store = Store(root / "data" / "news.db")

        def row(i, title, **kw):
            return {"id": i, "title": title, "link": "http://x/" + i, "summary": "", "source": "S", "src_id": "a", "cat": "biz",
                    "lang": "ja", "published": NOW - 100, "first_seen": NOW, **kw}

        self.store.upsert_items([
            row("1", "オリックス・バファローズが連勝"),
            row("2", "オリックスが最高益、通期予想を上方修正"),
            row("3", "ソフトバンクグループが投資を発表"),
            row("4", "全く関係ない話題", via=["ソフトバンク"]),
            row("5", "オリックス:剰余金の配当に関するお知らせ", src_id="td", cat="disclosure", company="オリックス"),
            row("6", "福岡ソフトバンクホークスが勝利"),
        ])
        self.payload = export.build_payload(self.store, WATCH, NOW, default_cats={"a": "biz", "td": "disclosure"})
        self.by_title = {i["title"]: i for i in self.payload["items"]}

    def test_stock_tags_kind_and_tier(self):
        it = self.by_title["オリックスが最高益、通期予想を上方修正"]
        self.assertEqual(it["stocks"], [{"n": "オリックス", "r": "certain"}])
        self.assertEqual(it["kind"], "earnings")
        self.assertEqual(it["tier"], "critical")
        it = self.by_title["ソフトバンクグループが投資を発表"]
        self.assertEqual([s["n"] for s in it["stocks"]], ["ソフトバンクグループ"])   # 「ソフトバンク」には付かない

    def test_excluded_articles_are_listed_for_checking_and_not_tagged(self):
        self.assertEqual(self.by_title["オリックス・バファローズが連勝"]["stocks"], [])
        ex = self.payload["excluded"]
        self.assertIn("バファローズ", ex["オリックス"][0]["reason"])
        self.assertEqual(ex["ソフトバンク"][0]["title"], "福岡ソフトバンクホークスが勝利")

    def test_disclosure_gets_kind_and_tier_and_is_certain(self):
        it = self.by_title["剰余金の配当に関するお知らせ"] if "剰余金の配当に関するお知らせ" in self.by_title else \
            self.by_title["オリックス:剰余金の配当に関するお知らせ"]
        self.assertEqual(it["stocks"], [{"n": "オリックス", "r": "certain"}])
        self.assertEqual(it["kind"], "dividend")

    def test_search_hit_is_uncertain_and_removed_watch_names_are_ignored(self):
        it = self.by_title["全く関係ない話題"]
        self.assertEqual(it["stocks"], [{"n": "ソフトバンク", "r": "uncertain"}])
        p = export.build_payload(self.store, [w for w in WATCH if w["name"] != "ソフトバンク"], NOW, default_cats={"a": "biz"})
        self.assertEqual({i["title"]: i["stocks"] for i in p["items"]}["全く関係ない話題"], [])


if __name__ == "__main__":
    unittest.main()
