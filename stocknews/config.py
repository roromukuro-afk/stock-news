"""場所の決め方と、設定ファイル（取得元・ウォッチリスト）の読み書き。"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parent.parent

CATEGORIES = ("market", "disclosure", "macro", "biz", "world", "tech", "general")
LANGS = ("ja", "en")


@dataclass(frozen=True)
class Paths:
    root: Path = DEFAULT_ROOT

    @property
    def sources(self) -> Path:
        return self.root / "config" / "sources.json"

    @property
    def watchlist(self) -> Path:
        return self.root / "config" / "watchlist.json"

    @property
    def db(self) -> Path:
        return self.root / "data" / "news.db"

    @property
    def site(self) -> Path:
        return self.root / "site"

    @property
    def news_json(self) -> Path:
        return self.root / "site" / "data" / "news.json"


def atomic_write(path: Path, text: str) -> None:
    """書き込み途中の壊れたファイルを残さないよう、一時ファイル経由で置き換える。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_sources(paths: Paths) -> list[dict]:
    raw = json.loads(paths.sources.read_text(encoding="utf-8"))
    out: list[dict] = []
    seen: set[str] = set()
    for s in raw["sources"]:
        sid = s["id"]
        if sid in seen:
            raise ValueError(f"取得元の id が重複しています: {sid}")
        seen.add(sid)
        if s["cat"] not in CATEGORIES:
            raise ValueError(f"{sid}: cat が不正です: {s['cat']}")
        if s["lang"] not in LANGS:
            raise ValueError(f"{sid}: lang が不正です: {s['lang']}")
        if not (s.get("url") or s.get("gn")):
            raise ValueError(f"{sid}: url か gn が必要です")
        out.append(s)
    return out


# ---- ウォッチリスト ----------------------------------------------------------

_CODE_RE = re.compile(r"^[A-Za-z0-9.\-]{1,12}$")
MAX_WATCH = 60
MAX_EXCLUDE = 30


def _terms(raw, label: str, limit: int, min_len: int = 2) -> list[str]:
    """カンマ区切りの文字列または一覧を、重複のない語の一覧にする。"""
    if isinstance(raw, str):
        raw = re.split(r"[,、，\n]", raw)
    out: list[str] = []
    for a in raw or []:
        a = str(a).strip()
        if not a:
            continue
        if not min_len <= len(a) <= 40:
            raise ValueError(f"{label}は {min_len}〜40 文字で入力してください")
        if a.casefold() not in (x.casefold() for x in out):
            out.append(a)
    return out[:limit]


STATUSES = ("active", "paused")      # 監視中・停止中（停止中は取得も照合もしない）
MAX_KEYWORDS = 10


def _text(raw, label: str, limit: int) -> str:
    s = str(raw or "").strip()
    if len(s) > limit:
        raise ValueError(f"{label}は {limit} 文字以内で入力してください")
    return s


def _url(raw, label: str) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""
    if len(s) > 300 or not re.match(r"^https?://[^\s<>\"']+$", s, re.I):
        raise ValueError(f"{label}は http:// か https:// で始まる URL で入力してください")
    return s


def clean_watch(raw: dict) -> dict:
    name = str(raw.get("name", "")).strip()
    if not 2 <= len(name) <= 40:
        raise ValueError("名前は 2〜40 文字で入力してください")
    code = str(raw.get("code", "") or "").strip().upper()
    if code and not _CODE_RE.match(code):
        raise ValueError("コードは英数字（ハイフン・ドット可）12 文字以内で入力してください")
    aliases = [a for a in _terms(raw.get("aliases"), "別名", 8) if a.casefold() != name.casefold()]
    exclude = _terms(raw.get("exclude"), "除外語", MAX_EXCLUDE)
    # 開示レーダーの銘柄（stocks / stock_profiles）と同じ項目。無くても動く（古い銘柄リストもそのまま読める）
    status = str(raw.get("status", "") or "active").strip()
    if status not in STATUSES:
        raise ValueError("状態は 監視中（active）か 停止中（paused）にしてください")
    return {
        "name": name, "code": code,
        "name_en": _text(raw.get("name_en"), "英語名", 60),
        "status": status,
        "exchange": _text(raw.get("exchange"), "市場", 30),
        "sector": _text(raw.get("sector"), "業種", 30),
        "official_url": _url(raw.get("official_url"), "公式サイトの URL"),
        "ir_url": _url(raw.get("ir_url"), "IR 情報の URL"),
        "jp_keywords": _terms(raw.get("jp_keywords"), "キーワード（日本語）", MAX_KEYWORDS),
        "en_keywords": _terms(raw.get("en_keywords"), "キーワード（英語）", MAX_KEYWORDS),
        "aliases": aliases[:5], "exclude": exclude,
    }


def clean_watchlist(raw_list) -> list[dict]:
    if not isinstance(raw_list, list):
        raise ValueError("ウォッチリストは一覧で渡してください")
    out: list[dict] = []
    seen: set[str] = set()
    for raw in raw_list:
        if not isinstance(raw, dict):
            raise ValueError("ウォッチリストの項目が不正です")
        w = clean_watch(raw)
        key = w["name"].casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(w)
    if len(out) > MAX_WATCH:
        raise ValueError(f"ウォッチリストは {MAX_WATCH} 件までです")
    return out


def load_watchlist(paths: Paths) -> list[dict]:
    try:
        raw = json.loads(paths.watchlist.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    return clean_watchlist(raw)


def save_watchlist(paths: Paths, items: list[dict]) -> list[dict]:
    cleaned = clean_watchlist(items)
    atomic_write(paths.watchlist, json.dumps(cleaned, ensure_ascii=False, indent=2) + "\n")
    return cleaned
