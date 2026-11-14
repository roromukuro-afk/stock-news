"""自分の PC だけで動く小さなサーバー。画面を出し、一定の間隔でニュースを取り直す。"""
from __future__ import annotations

import json
import threading
import time
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from .config import Paths, clean_watchlist, load_watchlist, save_watchlist
from .export import write_export
from .refresh import run_refresh
from .store import Store

MAX_BODY = 20_000
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "stocknews"


class App:
    def __init__(self, paths: Paths, port: int, interval_min: int = 30, run_refresh_fn=run_refresh):
        self.paths = paths
        self.port = port
        self.interval = max(5, interval_min) * 60
        self.run_refresh = run_refresh_fn
        self._busy = threading.Lock()
        self.stop = threading.Event()
        self.state: dict = {"updating": False, "done": 0, "total": 0, "name": "", "error": None}
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    # ---- 取り直し ----
    def trigger(self) -> bool:
        """取り直しを始める。すでに動いているときは False。"""
        if not self._busy.acquire(blocking=False):
            return False
        self.state.update(updating=True, done=0, total=0, name="", error=None)
        threading.Thread(target=self._run, name="refresh", daemon=True).start()
        return True

    def _progress(self, done: int, total: int, name: str) -> None:
        self.state.update(done=done, total=total, name=name)

    def _run(self) -> None:
        try:
            self.run_refresh(self.paths, progress=self._progress)
            write_export(self.paths)
        except Exception as e:
            self.state["error"] = f"{type(e).__name__}: {e}"[:200]
        finally:
            self.state["updating"] = False
            self._busy.release()

    def loop(self) -> None:
        while not self.stop.is_set():
            store = Store(self.paths.db)
            last = int(store.get_meta("last_attempt", "0") or 0)
            if time.time() - last >= self.interval:
                self.trigger()
            self.stop.wait(30)

    def status(self) -> dict:
        store = Store(self.paths.db)
        lr = store.get_meta("last_refresh")
        la = store.get_meta("last_attempt")
        return {
            "updating": self.state["updating"],
            "done": self.state["done"],
            "total": self.state["total"],
            "name": self.state["name"],
            "error": self.state["error"],
            "last_refresh": int(lr) if lr else None,
            "last_attempt": int(la) if la else None,
            "interval_min": self.interval // 60,
        }


def make_handler(app: App):
    site = str(app.paths.site)

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=site, **kw)

        # ---- 共通 ----
        def log_message(self, fmt, *args):  # 画面の読み込みごとの記録は出さない
            pass

        def end_headers(self):
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            )
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def _json(self, obj, status=HTTPStatus.OK):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _host_ok(self) -> bool:
            # 他のサイトから名前を差し替えて叩かれる（DNS リバインディング）のを防ぐ
            return (self.headers.get("Host") or "").lower() in app.allowed_hosts

        def _post_ok(self) -> bool:
            if self.headers.get(CSRF_HEADER) != CSRF_VALUE:
                return False
            origin = self.headers.get("Origin")
            if origin and origin.lower() not in {f"http://{h}" for h in app.allowed_hosts}:
                return False
            return True

        def _read_json(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if not 0 < n <= MAX_BODY:
                raise ValueError("送られた内容の大きさが不正です")
            return json.loads(self.rfile.read(n).decode("utf-8"))

        # ---- ルーティング ----
        def do_GET(self):
            if not self._host_ok():
                return self._json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
            path = self.path.split("?", 1)[0]
            if path == "/api/status":
                return self._json(app.status())
            if path == "/api/watchlist":
                return self._json({"watchlist": load_watchlist(app.paths)})
            if path.startswith("/api/"):
                return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return super().do_GET()

        def do_HEAD(self):
            if not self._host_ok():
                self.send_response(HTTPStatus.FORBIDDEN)
                self.end_headers()
                return
            return super().do_HEAD()

        def do_POST(self):
            if not self._host_ok() or not self._post_ok():
                return self._json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
            path = self.path.split("?", 1)[0]
            try:
                if path == "/api/refresh":
                    return self._json({"started": app.trigger()})
                if path == "/api/watchlist":
                    body = self._read_json()
                    if not isinstance(body, dict):
                        raise ValueError("形式が不正です")
                    saved = save_watchlist(app.paths, clean_watchlist(body.get("watchlist")))
                    write_export(app.paths)           # 一致の印は書き出しのときに付くので、すぐ反映できる
                    started = app.trigger()           # 新しい銘柄のニュースは取り直して集める
                    return self._json({"watchlist": saved, "started": started})
            except (ValueError, json.JSONDecodeError) as e:
                return self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    return Handler


def serve(paths: Paths, port: int = 8770, interval_min: int = 30, open_browser: bool = False) -> None:
    app = App(paths, port, interval_min)
    if Store(paths.db).count_items():
        write_export(paths)  # 前回までの記事で、まず画面を出せるようにする
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    server.daemon_threads = True
    threading.Thread(target=app.loop, name="scheduler", daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    print(f"株ニュース: {url}  （{app.interval // 60} 分おきに自動で更新・Ctrl+C で終了）", flush=True)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop.set()
        server.server_close()
