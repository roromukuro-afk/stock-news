"""ニュース配信（RSS 2.0 / Atom / RDF）の取得と読み取り。標準ライブラリだけで動く。"""
from __future__ import annotations

import gzip
import html
import re
import socket
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

# 取得先から「機械の巡回」と見分けられるよう名前を付ける（Python の既定の名前は止められる配信がある）。
UA = "StockNewsReader/1.0 (personal feed reader)"
MAX_BYTES = 8_000_000
SUMMARY_MAX = 180


class FeedError(Exception):
    """取得・読み取りの失敗。画面の「取得状況」にそのまま出すので、日本語で短く書く。"""


def http_get(url: str, timeout: float = 20.0) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5",
        },
    )
    last: Exception | None = None
    for attempt in range(2):  # 一時的な失敗だけ 1 回やり直す
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise FeedError("サイズが大きすぎます")
            if data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            return data
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt == 0:
                last = e
                time.sleep(1.0)
                continue
            raise FeedError(f"HTTP {e.code}") from e
        except (socket.timeout, TimeoutError) as e:
            last = e
            if attempt == 0:
                time.sleep(1.0)
                continue
            raise FeedError("タイムアウト") from e
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                last = e
                if attempt == 0:
                    time.sleep(1.0)
                    continue
                raise FeedError("タイムアウト") from e
            raise FeedError(f"接続できません: {str(reason)[:60]}") from e
    raise FeedError(f"取得できません: {last}")


# ---- XML ---------------------------------------------------------------------

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_ENTITY = re.compile(r"&([A-Za-z][A-Za-z0-9]*);")
_BARE_AMP = re.compile(r"&(?!(?:#\d+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);)")
_XML_DECL = re.compile(r"^\s*<\?xml[^>]*\?>")
_XML_BUILTIN = {"amp", "lt", "gt", "quot", "apos"}


def _fix_entity(m: re.Match) -> str:
    if m.group(1) in _XML_BUILTIN:
        return m.group(0)
    u = html.unescape(m.group(0))  # &nbsp; など XML に無い名前を文字に直す
    return u if u != m.group(0) else "&amp;" + m.group(1) + ";"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def parse_xml(data: bytes) -> ET.Element:
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        pass
    # 配信によっては制御文字・未定義の実体参照・裸の & が混ざる。直せる範囲で直して 1 回だけ読み直す。
    text = data.decode("utf-8", errors="replace")
    text = _XML_DECL.sub("", text)
    text = _CTRL.sub("", text)
    text = _ENTITY.sub(_fix_entity, text)
    text = _BARE_AMP.sub("&amp;", text)
    try:
        return ET.fromstring(text)
    except ET.ParseError as e:
        head = data[:200].lstrip().lower()
        if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
            raise FeedError("配信ではなく HTML ページが返りました（URL が変わった可能性）") from e
        raise FeedError(f"XML を読めません: {str(e)[:60]}") from e


# ---- 文字・日付 ----------------------------------------------------------------

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def to_text(s: str | None) -> str:
    if not s:
        return ""
    s = _TAG.sub(" ", s)
    s = html.unescape(s)
    return _WS.sub(" ", s).strip()


def norm_for_compare(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower()
    return re.sub(r"[\s\W_]+", "", s)


def summary_if_useful(title: str, summary: str) -> str:
    """見出しの繰り返しにすぎない要約は捨て、長いものは短く切る。"""
    if not summary:
        return ""
    nt, ns = norm_for_compare(title), norm_for_compare(summary)
    if not ns or ns in nt or nt in ns:
        return ""
    if len(summary) > SUMMARY_MAX:
        summary = summary[: SUMMARY_MAX - 1].rstrip() + "…"
    return summary


_TZ_WORDS = {" JST": " +0900", " EST": " -0500", " EDT": " -0400", " CST": " -0600", " CDT": " -0500",
             " PST": " -0800", " PDT": " -0700"}


def parse_date(s: str | None) -> float | None:
    """日付を UNIX 秒に。時刻帯が無い日付は UTC とみなす。読めなければ None。"""
    s = (s or "").strip()
    if not s:
        return None
    s2 = s
    for k, v in _TZ_WORDS.items():
        if s2.endswith(k):
            s2 = s2[: -len(k)] + v
            break
    d: datetime | None
    try:
        d = parsedate_to_datetime(s2)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    try:
        return d.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


_TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|mc_|ref$|ref_src$|cmpid$|rss$|ncid$)", re.I)


def normalize_link(link: str) -> str:
    link = link.strip()
    # TDnet 配信の中継 URL（…/rd.php?https://…pdf）は、本来の URL に直す
    m = re.match(r"^https?://[^/]*yanoshin\.jp/rd\.php\?(https?://.+)$", link)
    if m:
        link = m.group(1)
    try:
        u = urllib.parse.urlsplit(link)
    except ValueError:
        return link
    q = [(k, v) for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True) if not _TRACKING.match(k)]
    return urllib.parse.urlunsplit((u.scheme, u.netloc, u.path, urllib.parse.urlencode(q), ""))


# ---- 記事の取り出し ---------------------------------------------------------------

def _child_text(el: ET.Element, names: tuple[str, ...]) -> str:
    for c in el:
        if _local(c.tag) in names:
            t = "".join(c.itertext()).strip() if len(c) else (c.text or "").strip()
            if t:
                return t
    return ""


def _link(el: ET.Element) -> str:
    fallback = ""
    for c in el:
        if _local(c.tag) != "link":
            continue
        href = c.get("href")
        if href:
            if c.get("rel", "alternate") == "alternate":
                return href.strip()
            fallback = fallback or href.strip()
        elif c.text and c.text.strip():
            return c.text.strip()
    if fallback:
        return fallback
    g = _child_text(el, ("guid", "id"))
    return g if g.startswith("http") else ""


def parse_feed(data: bytes) -> list[dict]:
    """記事の一覧を返す。各記事: title / link / summary / published(秒 or None) / source(任意)。"""
    root = parse_xml(data)
    kind = _local(root.tag).lower()
    if kind == "html":
        raise FeedError("配信ではなく HTML ページが返りました（URL が変わった可能性）")
    if kind not in ("rss", "rdf", "feed"):
        raise FeedError("配信の形式ではありません")
    out: list[dict] = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        title = to_text(_child_text(el, ("title",)))
        link = _link(el)
        if not title or not link:
            continue
        summary = to_text(_child_text(el, ("description", "summary", "encoded", "content")))
        published = parse_date(_child_text(el, ("pubDate", "date", "published", "updated", "issued")))
        source = to_text(_child_text(el, ("source",)))
        out.append({
            "title": title,
            "link": link,
            "summary": summary_if_useful(title, summary),
            "published": published,
            "source": source,
        })
    return out
