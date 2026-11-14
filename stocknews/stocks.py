"""銘柄ごとの記事分類。開示レーダー（kaiji-radar）の仕組みを、AI なし・通信なし（無料）の規則だけで移したもの。

記事 × 銘柄ごとに次を決める:
  関連性   certain（社名が見出しにある / 公式開示）・uncertain（要約だけ・コードだけ・検索で当たっただけ）・
           excluded（銘柄ごとの除外語に当たったノイズ。保護される記事は消さず uncertain で残す）
  種類     決算 / 業績予想修正 / 配当 / 自己株式 / M&A・TOB / 提携 / 人事 / 株主総会 / 訴訟・行政処分 / 製品・サービス / その他
  重要度   最重要 / 重要 / 通常（規則だけ）

開示レーダーとの違い: 関連性の最終判定に Gemini（AI）を使わない。迷う記事は uncertain として残し、
画面の「除外した記事」で確かめて、除外語を足して直す。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .textmatch import fold

ASCII_ONLY = re.compile(r"^[\x20-\x7e]+$")


def term_regex(term: str) -> re.Pattern:
    """ならした語（fold 済み）の照合。英数字だけの語は、前後が英数字なら別の語として当たらない
    （例: 「JT」が「JTB」に当たらない）。日本語を含む語は部分一致。"""
    if ASCII_ONLY.match(term):
        return re.compile(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])")
    return re.compile(re.escape(term))


# ---- 銘柄 ------------------------------------------------------------------------

@dataclass
class Stock:
    name: str
    code: str
    terms: list[str]                         # 社名・別名（fold 済み）
    term_res: list[re.Pattern]
    code_re: re.Pattern | None
    exclude: list[tuple[str, re.Pattern]]    # (表示用の語, 照合)
    masks: list[str] = field(default_factory=list)   # この銘柄の語を含む、より長い別銘柄の語（取り違え防止）

    def mask(self, text: str) -> str:
        for m in self.masks:
            text = text.replace(m, " ")
        return text

    def name_hit(self, text: str) -> bool:
        return any(r.search(text) for r in self.term_res)


def _code_regex(code: str) -> re.Pattern | None:
    c = fold(code)
    if not c:
        return None
    esc = re.escape(c)
    if re.fullmatch(r"[a-z]+", c):
        # 米国のティッカー。3 文字以上のときだけ（1〜2 文字は普通の語に当たりすぎる）
        return re.compile(r"(?<![a-z0-9])" + esc + r"(?![a-z0-9])") if len(c) >= 3 else None
    # 日本の銘柄コードは裸の 4 桁の数字で、西暦・型番・創設年（1911 など）に当たりすぎる。
    # 「(7203)」「【7203】」「コード 7203」「7203.T」「東証…7203」の形のときだけコードとみなす。
    return re.compile(
        r"[（(【\[]\s*" + esc + r"\s*[）)】\]]"
        r"|(?:証券コード|銘柄コード|コード|code|ticker|銘柄)\s*[:：]?\s*" + esc + r"(?![0-9a-z])"
        r"|(?<![0-9a-z])" + esc + r"\.t\b"
        r"|東証[^\s]{0,8}" + esc + r"(?![0-9a-z])"
    )


def build_matchers(watchlist: list[dict]) -> list[Stock]:
    stocks: list[Stock] = []
    for w in watchlist:
        terms = []
        for t in [w["name"]] + list(w.get("aliases", [])):
            f = fold(t)
            if f and f not in terms:
                terms.append(f)
        stocks.append(Stock(
            name=w["name"], code=w.get("code", ""), terms=terms, term_res=[term_regex(t) for t in terms],
            code_re=_code_regex(w.get("code", "")),
            exclude=[(x, term_regex(fold(x))) for x in w.get("exclude", []) if fold(x)],
        ))
    # 「ソフトバンク」と「ソフトバンクグループ」のように、ある銘柄の語が別の銘柄のより長い語の一部のとき、
    # 長い方が出てくる所は短い方の一致として数えない
    for s in stocks:
        for o in stocks:
            if o is s:
                continue
            for t2 in o.terms:
                if any(t1 != t2 and t1 in t2 for t1 in s.terms) and t2 not in s.terms and t2 not in s.masks:
                    s.masks.append(t2)
        s.masks.sort(key=len, reverse=True)
    return stocks


def match_watch(stocks: list[Stock], *texts: str) -> list[str]:
    """文字列のどれかに社名・別名・コードがある銘柄の名前（適時開示の絞り込みなど、大づかみの照合用）。"""
    blob = fold(" ".join(t for t in texts if t))
    hits = []
    for s in stocks:
        b = s.mask(blob)
        if s.name_hit(b) or (s.code_re is not None and s.code_re.search(b)):
            hits.append(s.name)
    return hits


# ---- 保護キーワードとノイズ（開示レーダーの protection.ts / 全銘柄共通ルール） ----------------------

# これらが記事にあれば、除外語に当たっても消さず「関連不確実」で残す（IR・企業リスクを誤って消さない）。
# 設計: 「決算」「IR」「生産」「契約」の単独は使わない（大決算セール・firm・生産終了の中古品・契約更改を保護してしまうため）。
PROTECT_KEYWORDS = [
    "決算短信", "決算発表", "決算説明会", "四半期決算", "通期決算",
    "IR情報", "IR資料", "IR発表", "適時開示", "TDnet", "EDINET", "有価証券報告書", "臨時報告書",
    "大量保有", "TOB", "公開買付", "配当", "自己株", "株式分割", "増資",
    "下方修正", "上方修正", "業績予想",
    "M&A", "買収", "資本提携", "業務提携", "子会社",
    "行政処分", "訴訟", "不祥事", "リコール", "製品安全", "品質問題",
    "生産停止", "生産台数", "生産ライン", "生産計画", "工場",
    "販売台数", "受注", "公式発表", "プレスリリース",
    "株主総会", "株価", "業績", "純利益", "営業利益", "経常利益", "売上高",
    "格付", "出資", "売却", "譲渡", "経営", "代表取締役", "ガバナンス",
]
_PROTECT_RES = [term_regex(fold(k)) for k in PROTECT_KEYWORDS]

# 全銘柄共通のノイズ: 市場調査レポート業者の宣伝記事（「○○市場 2032年までの規模・CAGR」など）
_MARKET_REPORT = re.compile(
    r"cagr|market (?:size|research|report|analysis|forecast)|市場調査レポート|市場規模.{0,20}(?:20[3-9]\d|予測)|"
    r"(?:20[3-9]\d)年まで.{0,12}市場"
)


def protected_by(blob: str) -> str | None:
    for kw, r in zip(PROTECT_KEYWORDS, _PROTECT_RES):
        if r.search(blob):
            return kw
    return None


# ---- 関連性 ----------------------------------------------------------------------------------

CERTAIN, UNCERTAIN, EXCLUDED = "certain", "uncertain", "excluded"


def assess(stock: Stock, title: str, summary: str, company: str | None, *, via: bool, safe: bool) -> tuple[str | None, str]:
    """記事と銘柄の関連性。戻り値は (certain / uncertain / excluded / None, 理由)。None は無関係（その銘柄の記事ではない）。

    safe は公式の開示（TDnet）。公式の開示は、会社名が合えば確実で、絶対に除外しない。"""
    t = stock.mask(fold(title))
    s = stock.mask(fold(summary))
    c = stock.mask(fold(company or ""))
    name_in_title = stock.name_hit(t) or (bool(c) and stock.name_hit(c))
    if safe and name_in_title:
        return CERTAIN, ""
    if name_in_title:
        base = CERTAIN
    elif stock.name_hit(s):
        base = UNCERTAIN
    elif stock.code_re is not None and (stock.code_re.search(t) or stock.code_re.search(s)):
        base = UNCERTAIN
    elif via:
        base = UNCERTAIN   # Google ニュースがその銘柄の検索で返したが、見出し・要約には名前が無い
    else:
        return None, ""
    blob = t + " " + s
    reason = ""
    for word, r in stock.exclude:
        if r.search(blob):
            reason = f"除外語「{word}」"
            break
    if not reason and not safe and _MARKET_REPORT.search(blob):
        reason = "市場調査レポートの宣伝"
    if reason:
        keep = protected_by(blob)
        if keep:
            return UNCERTAIN, f"{reason}に当たるが、保護語「{keep}」があるため残す"
        return EXCLUDED, reason
    return base, ""


# ---- 種類（開示レーダーの event-type.ts） --------------------------------------------------------

KIND_LABELS = {
    "earnings": "決算", "guidance": "業績予想修正", "dividend": "配当", "buyback": "自己株式",
    "ma": "M&A・TOB", "alliance": "提携", "personnel": "人事", "agm": "株主総会",
    "legal": "訴訟・行政処分", "product": "製品・サービス", "other": "その他",
}
KIND_ORDER = list(KIND_LABELS)

# 上から順に見て、最初に当たったものを採る
_KIND_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile("決算短信|決算発表|決算説明会|四半期決算|通期決算|中間決算|純利益|営業利益|経常利益|売上高|最高益|増益|減益|(?<!大)決算"), "earnings"),
    (re.compile("業績予想|業績修正|予想の修正|上方修正|下方修正"), "guidance"),
    (re.compile("配当|増配|減配|無配|優待"), "dividend"),
    (re.compile("自己株式|自社株買い|自社株|株式分割"), "buyback"),
    (re.compile(r"TOB|公開買付|買収|合併|子会社化|M&A|経営統合|株式交換", re.I), "ma"),
    (re.compile("資本提携|業務提携|資本業務提携|戦略的提携|提携"), "alliance"),
    (re.compile("代表取締役|取締役.*(選任|退任|異動)|人事異動|役員人事|新社長|CEO交代|社長交代|社長に就任"), "personnel"),
    (re.compile("株主総会"), "agm"),
    (re.compile("訴訟|提訴|行政処分|課徴金|リコール|不祥事|品質問題|不正|業務停止|摘発|情報漏洩|情報漏えい"), "legal"),
    (re.compile("新製品|新サービス|発売|提供開始|受注|契約締結|開始へ|参入|発表会|新型"), "product"),
]


def event_kind(title: str, summary: str = "") -> str:
    text = fold(f"{title} {summary}")
    for rx, kind in _KIND_RULES:
        if rx.search(text):
            return kind
    return "other"


# ---- 重要度（開示レーダーの importance.ts・規則の部分だけ） -------------------------------------------

TIER_LABELS = {"critical": "最重要", "important": "重要", "normal": "通常"}

_CRITICAL: list[tuple[re.Pattern, str]] = [
    (re.compile("上方修正|下方修正|業績予想.{0,10}修正|業績修正"), "業績予想の修正"),
    (re.compile("無配|減配|増配|配当予想.*(修正|変更)"), "配当変更・無配・増配"),
    (re.compile(r"TOB|公開買付|合併|会社分割|株式交換|株式移転|経営統合", re.I), "TOB・M&A・会社分割"),
    (re.compile("行政処分|課徴金|業務停止命令|不祥事|訴訟|提訴|損害賠償請求"), "行政処分・不祥事・訴訟"),
    (re.compile("代表取締役.*(交代|辞任|退任|解任|新任|就任)|新社長|新CEO|CEO交代|社長交代"), "代表者変更"),
    (re.compile("上場廃止|監理銘柄|整理銘柄"), "上場廃止・監理銘柄関連"),
]
_IMPORTANT: list[tuple[re.Pattern, str]] = [
    (re.compile("決算短信|中間決算|通期決算|最高益"), "決算"),
    (re.compile("月次売上|月次業績|月次動向"), "月次業績"),
    (re.compile("資本業務提携|業務提携|資本提携|戦略的提携"), "資本・業務提携"),
    (re.compile("大口契約|大型契約|大口受注"), "大口契約"),
    (re.compile("新工場|工場新設|生産停止|生産再開|工場稼働"), "新工場・生産停止"),
    (re.compile("株主総会"), "株主総会"),
    (re.compile("株式分割"), "株式分割"),
    (re.compile("格付.*(変更|引き上げ|引き下げ|見直し)|格付会社"), "格付変更"),
    (re.compile("子会社化|子会社(の)?異動|子会社(の)?移管"), "主要子会社の変更"),
    (re.compile("自己株式|自社株買い"), "自己株式取得"),
    (re.compile("特別損失|最終赤字|純損失|営業損失"), "赤字・特別損失"),
]


def importance(title: str, summary: str = "", *, safe: bool = False) -> tuple[str, str]:
    text = fold(f"{title} {summary}")
    for rx, reason in _CRITICAL:
        if rx.search(text):
            return "critical", reason
    for rx, reason in _IMPORTANT:
        if rx.search(text):
            return "important", reason
    return ("important", "公式開示") if safe else ("normal", "")
