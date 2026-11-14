"""Cloudflare Pages に置く（合言葉つき）。スマホで見るための公開で、手元の画面とは別に動く。

    python -m stocknews publish setup      初回だけ: トークンの確認・置き場所の作成・合言葉の登録
    python -m stocknews publish deploy     取得して書き出し、公開先に置く（--no-fetch で取得を省く）
    python -m stocknews publish check      公開先で、合言葉なし=401・違う合言葉=401・正しい合言葉=200 と、ログイン画面・Cookie を確かめる
    python -m stocknews publish password   合言葉を表示する（自分が見るときだけ）

必要な値は .env（git 管理外）に入れる。表示もチャットへの貼り付けもしない:
    CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN（Cloudflare Pages: Edit の権限）
    SITE_PASSWORD と CLOUDFLARE_PAGES_PROJECT は setup が作る。

守り方: functions/_middleware.js が全部の要求（画面もデータも）で合言葉を確かめる。入り方は、ログイン画面（/login）で
入れて長持ちする Cookie をもらう（スマホのホーム画面のアプリで毎回聞かれないように）か、Basic 認証（道具用）。
合言葉を Cloudflare に登録してからでないと置かない（deploy が確かめる）ので、置いた瞬間から保護されている。
公開先に置くのは取得済みのデータの写し。取り直しは手元の PC で行う（公開先は自分では取得しない）。
"""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shutil
import string
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from .config import Paths

API = "https://api.cloudflare.com/client/v4"
UA = "stock-news-publish/1.0"   # Python の既定の名前は Cloudflare に error 1010 (403) で止められる
ENV_KEYS = ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN", "SITE_PASSWORD", "CLOUDFLARE_PAGES_PROJECT")
# 公開の実行ログ（GitHub Actions）に出さない値。トークン・合言葉のほか、アカウント ID と公開先の名前も伏せる
SECRET_KEYS = ENV_KEYS


# ---- .env ----------------------------------------------------------------------

def read_env(paths: Paths) -> dict[str, str]:
    """.env（手元）と環境変数（GitHub Actions の秘密）を合わせる。環境変数があればそちらを優先。"""
    p = paths.root / ".env"
    out: dict[str, str] = {}
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    for k in ENV_KEYS:
        if os.environ.get(k):
            out[k] = os.environ[k]
    return out


def set_env_value(paths: Paths, key: str, value: str) -> None:
    p = paths.root / ".env"
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    for i, ln in enumerate(lines):
        if ln.startswith(key + "="):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def need(env: dict[str, str], *names: str) -> None:
    missing = [n for n in names if not env.get(n)]
    if missing:
        raise SystemExit(f".env（または環境変数）に {', '.join(missing)} が入っていません。")


def mask(text: str, env: dict[str, str]) -> str:
    for k in SECRET_KEYS:
        if env.get(k):
            text = text.replace(env[k], "***")
    return text


def new_password() -> str:
    """打ちやすい合言葉（小文字と数字・4 文字ずつ 4 組＝約 82 ビット）。紛らわしい文字は除く。"""
    alphabet = "".join(c for c in string.ascii_lowercase + string.digits if c not in "0o1l")
    return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(4))


# ---- Cloudflare の API ----------------------------------------------------------

def api(env: dict[str, str], method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        API + path, method=method, data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {env['CLOUDFLARE_API_TOKEN']}", "Content-Type": "application/json",
                 "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:  # noqa: BLE001
            return e.code, {}


def errors(js: dict) -> str:
    return "; ".join(f"{e.get('code')}: {e.get('message')}" for e in js.get("errors", [])) or "(詳細なし)"


def password_registered(env: dict[str, str]) -> bool:
    acct, name = env["CLOUDFLARE_ACCOUNT_ID"], env["CLOUDFLARE_PAGES_PROJECT"]
    st, js = api(env, "GET", f"/accounts/{acct}/pages/projects/{name}")
    prod = ((js.get("result") or {}).get("deployment_configs") or {}).get("production") or {}
    return st == 200 and "SITE_PASSWORD" in (prod.get("env_vars") or {})


# ---- 各コマンド -------------------------------------------------------------------

def setup(paths: Paths) -> int:
    env = read_env(paths)
    need(env, "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN")
    acct = env["CLOUDFLARE_ACCOUNT_ID"]
    st, js = api(env, "GET", "/user/tokens/verify")
    if st != 200:
        st, js = api(env, "GET", f"/accounts/{acct}/tokens/verify")
    status = (js.get("result") or {}).get("status")
    print("トークンの確認:", status or f"失敗 (HTTP {st}) {errors(js)}")
    if status != "active":
        return 1

    if not env.get("SITE_PASSWORD"):
        set_env_value(paths, "SITE_PASSWORD", new_password())
        env = read_env(paths)
        print("合言葉を作って .env に入れました（表示は `publish password`）")

    name = env.get("CLOUDFLARE_PAGES_PROJECT", "")
    if not name:
        for _ in range(6):
            cand = "stock-news-" + "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(5))
            st, js = api(env, "POST", f"/accounts/{acct}/pages/projects", {"name": cand, "production_branch": "main"})
            if st == 200 and js.get("success"):
                name = cand
                break
            print(f"  {cand}: 作れませんでした (HTTP {st}) {errors(js)}")
        if not name:
            return 1
        set_env_value(paths, "CLOUDFLARE_PAGES_PROJECT", name)
        env = read_env(paths)
        print("置き場所を作りました:", name)
    else:
        st, js = api(env, "GET", f"/accounts/{acct}/pages/projects/{name}")
        print("置き場所:", name, "既にあります" if st == 200 else f"見つかりません (HTTP {st})")
        if st != 200:
            return 1

    st, js = api(env, "PATCH", f"/accounts/{acct}/pages/projects/{name}", {
        "deployment_configs": {"production": {"env_vars": {
            "SITE_PASSWORD": {"type": "secret_text", "value": env["SITE_PASSWORD"]}}}}})
    ok = st == 200 and bool(js.get("success"))
    print("合言葉の登録:", "完了" if ok else f"失敗 (HTTP {st}) {errors(js)}")
    print("登録の確認:", "合言葉が登録されています" if password_registered(env) else "登録が見当たりません")
    return 0 if ok and password_registered(env) else 1


def deploy(paths: Paths, fetch: bool = True, only_if_new: bool = False) -> int:
    env = read_env(paths)
    need(env, "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_PAGES_PROJECT")
    if not password_registered(env):
        raise SystemExit("公開先に合言葉が登録されていません。先に `publish setup` を実行してください（保護なしでは置きません）。")
    from .export import write_export
    if fetch:
        from .refresh import run_refresh
        res = run_refresh(paths)
        print(f"取得: 取得元 {res['sources']} 件中 {res['ok']} 件成功・新しい記事 {res['new']} 件")
        if not res["ok"]:
            raise SystemExit("どの取得元からも取得できませんでした。古いデータを置かないよう、中止します。")
        if only_if_new and res["new"] == 0:
            print("新しい記事がないので、置き直しません。")
            return 0
    payload = write_export(paths)
    print(f"書き出し: {len(payload['items'])} 件")
    npx = shutil.which("npx")
    if not npx:
        raise SystemExit("npx（Node.js）が見つかりません。")
    run_env = {**os.environ, "CLOUDFLARE_API_TOKEN": env["CLOUDFLARE_API_TOKEN"],
               "CLOUDFLARE_ACCOUNT_ID": env["CLOUDFLARE_ACCOUNT_ID"], "WRANGLER_SEND_METRICS": "false"}
    p = subprocess.run([npx, "--yes", "wrangler@4", "pages", "deploy", "site", "--project-name",
                        env["CLOUDFLARE_PAGES_PROJECT"], "--branch", "main", "--commit-dirty=true"],
                       cwd=paths.root, env=run_env, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=900)
    out = mask((p.stdout or "") + (p.stderr or ""), env)
    print("\n".join(out.splitlines()[-12:]))
    if p.returncode == 0:
        urls = deployment_urls((p.stdout or "") + (p.stderr or ""), env["CLOUDFLARE_PAGES_PROJECT"])
        if urls:
            print("この配置の URL:", mask(urls[0], env))
    return p.returncode


def sync_watchlist(paths: Paths) -> int:
    """銘柄リスト（config/watchlist.json）を、GitHub Actions が使う秘密の変数 WATCHLIST_JSON に送る。
    リポジトリは公開なので、銘柄リストはファイルとして置かず、秘密の変数で渡す。"""
    gh = shutil.which("gh")
    if not gh:
        raise SystemExit("gh（GitHub CLI）が見つかりません。")
    from .config import clean_watchlist
    data = paths.watchlist.read_text(encoding="utf-8")
    clean_watchlist(json.loads(data))   # 壊れた内容を送らない
    p = subprocess.run([gh, "secret", "set", "WATCHLIST_JSON"], input=data, cwd=paths.root, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=120)
    print((p.stdout or p.stderr or "").strip()[:200] or ("送りました" if p.returncode == 0 else "失敗しました"))
    return p.returncode


def deployment_urls(text: str, project: str) -> list[str]:
    return re.findall(r"https://[a-z0-9]+\.%s\.pages\.dev" % re.escape(project), text)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None          # 転送を追わず、3xx の応答そのものを見る


def _raw(url: str, method: str = "GET", headers: dict | None = None, data: bytes | None = None):
    """転送を追わずに 1 回だけ要求する。戻り値は (状態, 応答ヘッダー)。接続できなければ (None, 理由)。"""
    req = urllib.request.Request(url, method=method, data=data, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=60) as r:
            return r.status, r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.headers
    except (urllib.error.URLError, OSError) as e:
        return None, str(getattr(e, "reason", e))[:50]


def login_checks(base: str, password: str) -> list[tuple[str, object, bool | None]]:
    """ログイン画面と Cookie が本番で想定どおりか。戻り値は (名前, 状態, 想定どおりか)。接続できなければ None。"""
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    out: list[tuple[str, object, bool | None]] = []

    def add(label, got, want, extra=True):
        code = got[0]
        out.append((label, code if code is not None else f"接続できません({got[1]})", None if code is None else (code == want and extra)))
        return got

    r = add("画面を開く（合言葉なし）→ログイン画面へ", _raw(base + "/", headers={"Accept": "text/html"}), 303)
    if r[0] == 303 and not (r[1].get("Location") or "").endswith("/login"):
        out[-1] = (out[-1][0], 303, False)
    add("ログイン画面", _raw(base + "/login", headers={"Accept": "text/html"}), 200)
    bad = add("違う合言葉でログイン", _raw(base + "/login", "POST", form, urllib.parse.urlencode({"password": "wrong-password"}).encode()), 401)
    if bad[0] == 401 and bad[1].get("Set-Cookie"):
        out[-1] = (out[-1][0], 401, False)
    good = add("正しい合言葉でログイン", _raw(base + "/login", "POST", form, urllib.parse.urlencode({"password": password}).encode()), 303)
    cookie = ((good[1].get("Set-Cookie") if good[0] == 303 else "") or "").split(";")[0]
    if good[0] == 303 and not cookie.startswith("sn_auth="):
        out[-1] = (out[-1][0], 303, False)
    if cookie.startswith("sn_auth="):
        add("Cookie だけでデータを取る", _raw(base + "/data/news.json", headers={"Cookie": cookie}), 200)
        add("改ざんした Cookie は通らない", _raw(base + "/data/news.json", headers={"Cookie": cookie[:-1] + ("0" if cookie[-1] != "0" else "1")}), 401)
    else:
        out.append(("Cookie だけでデータを取る", "Cookie が付きませんでした", False))
    return out


def check(paths: Paths, extra_urls: list[str] | None = None) -> int:
    env = read_env(paths)
    need(env, "CLOUDFLARE_PAGES_PROJECT", "SITE_PASSWORD")
    good = "Basic " + base64.b64encode(f"me:{env['SITE_PASSWORD']}".encode()).decode()
    bad = "Basic " + base64.b64encode(b"me:wrong-password").decode()
    main_base = f"https://{env['CLOUDFLARE_PAGES_PROJECT']}.pages.dev"
    # 本番の URL は必ず確かめる。配置ごとの URL（--url）と http は、接続できなければ「未確認」として報告する
    # （新しい Pages プロジェクトのサブドメインは、証明書が整うまで数分かかる。中身が見えたわけではない）
    targets = [(main_base, True), (main_base.replace("https://", "http://"), False)] + [(u.rstrip("/"), False) for u in (extra_urls or [])]
    ok = True
    unverified = 0
    for base, required in targets:
        plain_http = base.startswith("http://")
        for path in ("/", "/app.js", "/data/news.json"):
            res = []
            cases = (("合言葉なし", None, 401),) if plain_http else (
                ("合言葉なし", None, 401), ("違う合言葉", bad, 401), ("正しい合言葉", good, 200))
            for label, hdr, want in cases:
                req = urllib.request.Request(base + path, headers={"User-Agent": UA, **({"Authorization": hdr} if hdr else {})})
                try:
                    with urllib.request.urlopen(req, timeout=60) as r:
                        code = r.status
                except urllib.error.HTTPError as e:
                    code = e.code
                except (urllib.error.URLError, OSError) as e:
                    code = None
                    err = str(getattr(e, "reason", e))[:50]
                if code is None:
                    res.append((label, f"接続できません({err})", None))
                else:
                    res.append((label, code, code == want))
            if any(r[2] is None for r in res):
                unverified += 1
                ok &= not required
            else:
                ok &= all(r[2] for r in res)
            print(base.replace("https://", ""), path, " / ".join(
                f"{a} {b}{'' if c in (True, None) else ' ←想定外'}" for a, b, c in res))
    # ログイン画面と Cookie（本番だけ）
    lres = login_checks(main_base, env["SITE_PASSWORD"])
    for label, code, good_ in lres:
        print(f"{main_base.replace('https://', '')} ログイン: {label} {code}{'' if good_ is not False else ' ←想定外'}")
    ok &= all(g is not False for _, _, g in lres) and not any(g is None for _, _, g in lres)
    print("結果:", "合言葉が効いています" if ok and not unverified else
          ("合言葉が効いています（接続できず未確認のものがあります）" if ok else "想定と違う応答、または本番に接続できません"))
    return 0 if ok else 1


def show_password(paths: Paths) -> int:
    env = read_env(paths)
    need(env, "SITE_PASSWORD", "CLOUDFLARE_PAGES_PROJECT")
    print(f"URL: https://{env['CLOUDFLARE_PAGES_PROJECT']}.pages.dev/")
    print(f"合言葉: {env['SITE_PASSWORD']}   （ユーザー名は何でもよい）")
    return 0
