"""使い方:
  python -m stocknews serve [--port 8770] [--interval 30] [--open]   画面を出して自動で更新する
  python -m stocknews build                                          1 回だけ取得して書き出す
  python -m stocknews export                                         保存済みの記事から書き出すだけ
  python -m stocknews sources                                        取得元の一覧
  python -m stocknews publish setup|deploy|check|password|sync-watchlist スマホ用に Cloudflare へ置く（合言葉つき）
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime

from .config import Paths, load_sources, load_watchlist
from .export import write_export
from .refresh import run_refresh, watch_sources


def _cmd_build(paths: Paths) -> int:
    def progress(done: int, total: int, name: str) -> None:
        print(f"\r取得中 {done}/{total}  {name[:30]:<30}", end="", flush=True)

    res = run_refresh(paths, progress=progress)
    print()
    payload = write_export(paths)
    print(f"取得元 {res['sources']} 件中 {res['ok']} 件成功・新しい記事 {res['new']} 件・{res['seconds']} 秒")
    for sid, err in res["failed"].items():
        print(f"  失敗: {sid}: {err}")
    print(f"書き出し: {paths.news_json}（{len(payload['items'])} 件）")
    return 0 if res["ok"] else 1


def _cmd_sources(paths: Paths) -> int:
    sources = load_sources(paths)
    for s in sources:
        kind = "検索" if s.get("gn") else "配信"
        print(f"{s['id']:<16} {s['cat']:<10} {s['lang']}  {kind}  {s['name']}")
    wl = load_watchlist(paths)
    print(f"\n取得元 {len(sources)} 件 ＋ ウォッチリスト {len(wl)} 件（検索 {len(watch_sources(wl))} 本）")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(prog="stocknews", description="株式投資まわりのニュースを集める個人用ツール")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve", help="画面を出して自動で更新する")
    sp.add_argument("--port", type=int, default=8770)
    sp.add_argument("--interval", type=int, default=30, help="取り直す間隔（分・5 以上）")
    sp.add_argument("--open", action="store_true", help="ブラウザーを開く")
    sub.add_parser("build", help="1 回だけ取得して書き出す")
    sub.add_parser("export", help="保存済みの記事から書き出すだけ")
    sub.add_parser("sources", help="取得元の一覧")
    pp = sub.add_parser("publish", help="Cloudflare Pages に置く（合言葉つき）")
    pp.add_argument("action", choices=["setup", "deploy", "check", "password", "sync-watchlist"])
    pp.add_argument("--no-fetch", action="store_true", help="deploy: 取得せず、保存済みの記事から置く")
    pp.add_argument("--only-if-new", action="store_true", help="deploy: 新しい記事が無ければ置き直さない")
    pp.add_argument("--url", action="append", default=[], help="check: 確かめる URL を足す（配置ごとの URL など）")
    args = ap.parse_args(argv)
    paths = Paths()

    if args.cmd == "serve":
        from .server import serve
        try:
            serve(paths, args.port, args.interval, args.open)
        except OSError as e:
            print(f"起動できません（ポート {args.port} がすでに使われている可能性）: {e}", file=sys.stderr)
            return 1
        return 0
    if args.cmd == "build":
        return _cmd_build(paths)
    if args.cmd == "export":
        payload = write_export(paths)
        print(f"書き出し: {paths.news_json}（{len(payload['items'])} 件・{datetime.fromtimestamp(time.time()):%H:%M}）")
        return 0
    if args.cmd == "sources":
        return _cmd_sources(paths)
    if args.cmd == "publish":
        from . import publish
        if args.action == "setup":
            return publish.setup(paths)
        if args.action == "deploy":
            return publish.deploy(paths, fetch=not args.no_fetch, only_if_new=args.only_if_new)
        if args.action == "check":
            return publish.check(paths, args.url)
        if args.action == "sync-watchlist":
            return publish.sync_watchlist(paths)
        return publish.show_password(paths)
    return 2


if __name__ == "__main__":
    sys.exit(main())
