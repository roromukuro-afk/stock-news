"""文字のならし、似た見出しの束ね（同じ話題の判定）。"""
from __future__ import annotations

import re
import unicodedata


def fold(s: str) -> str:
    """全角・半角、大文字・小文字の差をならす。"""
    return unicodedata.normalize("NFKC", s).casefold()


# ---- 似た見出しの束ね ------------------------------------------------------------

_CJK = re.compile(r"[぀-ヿ㐀-鿿]+")
_WORD = re.compile(r"[a-z0-9]{3,}")
_STOP = {"the", "and", "for", "with", "from", "that", "this", "its", "are", "was", "has", "have", "will", "amid", "after",
         "over", "says", "say", "new", "how", "why", "what", "who", "not", "but", "you", "your", "can", "may", "more"}


def title_tokens(title: str) -> frozenset[str]:
    t = fold(title)
    toks: set[str] = set()
    for run in _CJK.findall(t):
        if len(run) == 1:
            toks.add(run)
        else:
            toks.update(run[i:i + 2] for i in range(len(run) - 1))
    toks.update(w for w in _WORD.findall(t) if w not in _STOP)
    return frozenset(toks)


_BRACKET = re.compile(r"【([^】]*)】")
_DAY = re.compile(r"(\d{1,2})日")


def guard_key(title: str, company: str | None = None, stocks=()) -> tuple[frozenset[str], frozenset[str], frozenset[str], frozenset[str]]:
    """定型の連載（「週間ランキング【値上がり率】」と「【値下がり率】」、「13日の…」と「12日の…」）や、
    同じ件名で会社だけ違う開示、見出しが似た別の銘柄の決算記事を取り違えないための手がかり。
    【】の中身・「○日」・会社名・結びついた銘柄を取り出す。"""
    t = unicodedata.normalize("NFKC", title)
    return (frozenset(_BRACKET.findall(t)), frozenset(_DAY.findall(t)), frozenset([fold(company)] if company else []),
            frozenset(stocks))


def guards_compatible(a: tuple, b: tuple) -> bool:
    # 【】・日・会社名は、どちらかが空なら食い違いとはみなさない。銘柄の組は厳密に同じでなければ束ねない
    # （ある銀行の記事に、見出しが似ているだけの別の銀行の記事を混ぜて、★を引き継がせないため）
    return all(not x or not y or x == y for x, y in zip(a[:3], b[:3])) and a[3] == b[3]


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


def cluster_items(items: list[dict], *, threshold: float = 0.5, min_shared: int = 4, window_s: int = 48 * 3600) -> list[list[dict]]:
    """新しい順に並んだ記事を、似た見出しごとにまとめる。先頭が代表、残りが「ほか」。

    見出しの文字（日本語は 2 文字ずつ、英語は単語）の重なりが threshold 以上で、
    言語が同じで、48 時間以内のものだけを同じ話題とみなす（別の出来事を混ぜないため厳しめ）。
    """
    groups: list[list[dict]] = []
    leads: list[tuple[frozenset[str], dict, tuple]] = []   # 各グループの代表の (語, 記事, 手がかり)
    index: dict[str, list[int]] = {}                # 語 -> 代表の番号
    for it in items:
        toks = title_tokens((it.get("company") or "") + " " + it["title"])   # 会社が違えば別の話題
        guard = guard_key(it["title"], it.get("company"), it.get("stocks") or ())
        best, best_j = -1, 0.0
        if toks:
            counts: dict[int, int] = {}
            for t in toks:
                post = index.get(t)
                if post is None or len(post) > 300:   # ありふれた語は手がかりにしない
                    continue
                for gi in post:
                    counts[gi] = counts.get(gi, 0) + 1
            for gi, shared in counts.items():
                if shared < min_shared:
                    continue
                ltoks, lead, lguard = leads[gi]
                if lead["lang"] != it["lang"] or abs(lead["published"] - it["published"]) > window_s:
                    continue
                if not guards_compatible(guard, lguard):
                    continue
                j = jaccard(toks, ltoks)
                if j >= threshold and j > best_j:
                    best, best_j = gi, j
        if best >= 0:
            groups[best].append(it)
        else:
            groups.append([it])
            leads.append((toks, it, guard))
            for t in toks:
                index.setdefault(t, []).append(len(groups) - 1)
    return groups
