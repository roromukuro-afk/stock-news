"""カテゴリの決め方と、適時開示（TDnet）の絞り込み。AI は使わず、語句の規則だけで決める。"""
from __future__ import annotations

import re
import unicodedata

# 株式・市況の話題とみなす語（日本語）
_MARKET_JA = re.compile(
    "株価|株式|日経平均|ＴＯＰＩＸ|TOPIX|東証|ダウ平均|ナスダック|S&P|決算|上場|配当|増配|減配|自社株|株主|銘柄|"
    "ETF|NISA|ニーサ|IPO|時価総額|格付け|目標株価|上方修正|下方修正|増益|減益|最高益|赤字転落|TOB|公開買い?付け|MBO|"
    "株安|株高|株式相場|為替相場|先物|公募増資|株式分割|空売り|信用取引|投資信託|投信|ストップ高|ストップ安|プライム市場|"
    "グロース市場|スタンダード市場|証券会社|アナリスト|格上げ|格下げ|買収提案|業績予想|日本株|米国株|株式市場|大引け|前引け|"
    "終値|年初来高値|上場来高値|インサイダー"
)
# 株式・市況の話題とみなす語（英語）
_MARKET_EN = re.compile(
    r"\b(stocks?|shares|equit(?:y|ies)|s&p ?500|nasdaq|dow jones|the dow|nikkei|topix|earnings|ipo|dividends?|buybacks?|"
    r"market cap|wall street|rally|sell-?offs?|bull market|bear market|price target|downgrades?|upgrades?|guidance|"
    r"short sellers?|etfs?|hedge funds?|stock market|investors?|volatility|futures|treasury yields?)\b",
    re.I,
)
# 経済・政策の話題とみなす語
_MACRO_JA = re.compile(
    "金利|利上げ|利下げ|物価|インフレ|デフレ|為替|円安|円高|ＧＤＰ|GDP|雇用統計|関税|日銀|金融政策|財政|国債|長期金利|"
    "景気|賃上げ|最低賃金|消費者物価|貿易|金融庁|財務省|経済対策|補正予算|増税|減税|原油|商品市況"
)
_MACRO_EN = re.compile(
    r"\b(inflation|interest rates?|rate (?:cut|hike)s?|central bank|federal reserve|the fed|ecb|bank of japan|boj|"
    r"gdp|jobs report|unemployment|tariffs?|recession|monetary policy|fiscal|budget deficit|cpi|trade war|treasury)\b",
    re.I,
)

# 出所の既定が次のものは、語句で動かさない（開示・政策そのものの配信）
_FIXED = {"disclosure", "macro"}


def classify(default_cat: str, title: str, summary: str = "", lang: str = "ja") -> str:
    if default_cat in _FIXED:
        return default_cat
    text = unicodedata.normalize("NFKC", f"{title} {summary}")
    if lang == "ja":
        if _MARKET_JA.search(text):
            return "market"
        if _MACRO_JA.search(text):
            return "macro"
    else:
        if _MARKET_EN.search(text):
            return "market"
        if _MACRO_EN.search(text):
            return "macro"
    return default_cat


# 適時開示のうち、株価に影響しうるものだけを残すための語
_IMPORTANT_DISCLOSURE = re.compile(
    "決算短信|業績予想|配当予想|剰余金の配当|配当|自己株式|株式分割|株式併合|公開買付|TOB|ＴＯＢ|MBO|ＭＢＯ|上場廃止|"
    "新規上場|第三者割当|公募|売出し|合併|吸収分割|株式交換|株式移転|業務提携|資本提携|資本業務提携|特別損失|特別利益|"
    "監理銘柄|整理銘柄|債務超過|継続企業|代表取締役の異動|不適切な会計|月次|増資|特設注意市場|優待"
)


# 個別銘柄・投信・指数の値動きページ（Google ニュースの検索に混ざる）。記事ではないので除く
_NOISE_PAGE = re.compile(r"[：:]\s*(?:株価|基準価格|チャート|時系列|掲示板)|の指数情報・推移|基準価額・投資信託情報|【[0-9A-Za-z]{2,10}】\s*[：:]")


def is_noise(title: str) -> bool:
    return bool(_NOISE_PAGE.search(unicodedata.normalize("NFKC", title)))


# Yahoo!ファイナンス経由の記事は、見出しの末尾に元の媒体名が「(株探ニュース)」の形で付く
_AGGREGATORS = {"Yahoo!ファイナンス", "Yahoo!ニュース", "Yahoo! JAPAN"}
_TRAILING_PUBLISHER = re.compile(r"\s*[(（]([^()（）0-9０-９]{2,20})[)）]\s*$")


def split_trailing_publisher(source: str, title: str) -> tuple[str, str]:
    if source in _AGGREGATORS:
        m = _TRAILING_PUBLISHER.search(title)
        if m and not m.group(1).endswith(("版", "編", "上", "下", "号")):
            return m.group(1), title[: m.start()].rstrip()
    return source, title


def is_important_disclosure(title: str) -> bool:
    return bool(_IMPORTANT_DISCLOSURE.search(unicodedata.normalize("NFKC", title)))


def split_company(title: str) -> tuple[str | None, str]:
    """TDnet の「会社名:件名」を分ける。区切りが無ければ (None, 元の見出し)。"""
    m = re.match(r"^(.{1,30}?)[:：]\s*(.+)$", title)
    if not m:
        return None, title
    return m.group(1).strip(), m.group(2).strip()
