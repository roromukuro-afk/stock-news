"""公開用の補助（stocknews/publish.py）の試験。ネットワークは使わない。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stocknews import publish  # noqa: E402
from stocknews.config import Paths  # noqa: E402


def clean_env():
    """手元のシェルに公開用の環境変数があっても、試験に影響しないようにする。"""
    return mock.patch.dict(os.environ, {k: "" for k in publish.ENV_KEYS})


class PublishHelperTests(unittest.TestCase):
    def paths(self) -> Paths:
        return Paths(Path(tempfile.mkdtemp(prefix="stocknews_pub_")))

    def test_env_roundtrip_keeps_other_keys_and_comments(self):
        p = self.paths()
        (p.root / ".env").write_text("# メモ\nCLOUDFLARE_ACCOUNT_ID=abc\nOTHER=\"x y\"\n", encoding="utf-8")
        publish.set_env_value(p, "SITE_PASSWORD", "pw")
        publish.set_env_value(p, "CLOUDFLARE_ACCOUNT_ID", "zzz")
        with clean_env():
            env = publish.read_env(p)
        self.assertEqual(env, {"CLOUDFLARE_ACCOUNT_ID": "zzz", "OTHER": "x y", "SITE_PASSWORD": "pw"})
        self.assertIn("# メモ", (p.root / ".env").read_text(encoding="utf-8"))

    def test_environment_variables_win_over_the_file_and_work_without_a_file(self):
        p = self.paths()
        with mock.patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": "from-env", "CLOUDFLARE_PAGES_PROJECT": ""}):
            self.assertEqual(publish.read_env(p)["CLOUDFLARE_API_TOKEN"], "from-env")     # GitHub Actions: .env が無い
            (p.root / ".env").write_text("CLOUDFLARE_API_TOKEN=from-file\nCLOUDFLARE_PAGES_PROJECT=proj\n", encoding="utf-8")
            env = publish.read_env(p)
        self.assertEqual(env["CLOUDFLARE_API_TOKEN"], "from-env")
        self.assertEqual(env["CLOUDFLARE_PAGES_PROJECT"], "proj")     # 空の環境変数は無いものとして扱う

    def test_missing_env_file_is_empty_and_need_reports_names(self):
        p = self.paths()
        with clean_env():
            self.assertEqual(publish.read_env(p), {})
        with self.assertRaises(SystemExit) as cm:
            publish.need({}, "CLOUDFLARE_API_TOKEN")
        self.assertIn("CLOUDFLARE_API_TOKEN", str(cm.exception))

    def test_mask_hides_every_value_that_must_not_reach_a_public_log(self):
        env = {"CLOUDFLARE_API_TOKEN": "tok-123456", "SITE_PASSWORD": "ab12-cd34", "CLOUDFLARE_ACCOUNT_ID": "acct9999",
               "CLOUDFLARE_PAGES_PROJECT": "stock-news-abc12"}
        out = publish.mask("tok-123456 ab12-cd34 acct9999 https://1a2b.stock-news-abc12.pages.dev", env)
        for secret in env.values():
            self.assertNotIn(secret, out)

    def test_new_password_shape_and_uniqueness(self):
        pw = publish.new_password()
        self.assertRegex(pw, r"^[a-z2-9]{4}(-[a-z2-9]{4}){3}$")
        self.assertFalse(set(pw) & set("01lo"))   # 紛らわしい文字は使わない
        self.assertNotEqual(pw, publish.new_password())

    def test_deployment_url_extraction(self):
        text = "✨ Deployment complete! Take a peek over at https://1a2b3c4d.stock-news-abc12.pages.dev\nhttps://stock-news-abc12.pages.dev"
        self.assertEqual(publish.deployment_urls(text, "stock-news-abc12"), ["https://1a2b3c4d.stock-news-abc12.pages.dev"])


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="stocknews_dep_"))
        self.paths = Paths(self.root)
        # 合言葉の値は要らない（Cloudflare に登録されているかだけ確かめる）。CI でも秘密にしなくてよいように
        self.env = {"CLOUDFLARE_ACCOUNT_ID": "a", "CLOUDFLARE_API_TOKEN": "t", "CLOUDFLARE_PAGES_PROJECT": "proj"}

    def run_deploy(self, *, registered=True, new=3, ok=1, only_if_new=False):
        calls = {"wrangler": 0, "export": 0}

        def fake_refresh(paths, **kw):
            return {"sources": 5, "ok": ok, "new": new, "failed": {}, "purged": 0, "seconds": 0.1}

        def fake_export(paths, now=None):
            calls["export"] += 1
            return {"items": []}

        def fake_run(*a, **kw):
            calls["wrangler"] += 1
            return mock.Mock(returncode=0, stdout="Deployment complete! https://abc123.proj.pages.dev", stderr="")

        with mock.patch.dict(os.environ, self.env), \
                mock.patch.object(publish, "password_registered", lambda env: registered), \
                mock.patch("stocknews.refresh.run_refresh", fake_refresh), \
                mock.patch("stocknews.export.write_export", fake_export), \
                mock.patch.object(publish.shutil, "which", lambda name: "npx"), \
                mock.patch.object(publish.subprocess, "run", fake_run):
            rc = publish.deploy(self.paths, fetch=True, only_if_new=only_if_new)
        return rc, calls

    def test_refuses_without_registered_password(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_deploy(registered=False)
        self.assertIn("合言葉が登録されていません", str(cm.exception))

    def test_deploys_when_there_are_new_articles(self):
        rc, calls = self.run_deploy(new=3, only_if_new=True)
        self.assertEqual((rc, calls["wrangler"]), (0, 1))

    def test_skips_when_nothing_new_and_only_if_new(self):
        rc, calls = self.run_deploy(new=0, only_if_new=True)
        self.assertEqual((rc, calls["wrangler"], calls["export"]), (0, 0, 0))

    def test_always_deploys_without_only_if_new(self):
        rc, calls = self.run_deploy(new=0, only_if_new=False)
        self.assertEqual((rc, calls["wrangler"]), (0, 1))

    def test_stops_when_every_source_failed(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_deploy(ok=0)
        self.assertIn("古いデータを置かない", str(cm.exception))


class SyncWatchlistTests(unittest.TestCase):
    def test_rejects_broken_content_before_sending(self):
        root = Path(tempfile.mkdtemp(prefix="stocknews_sync_"))
        (root / "config").mkdir()
        (root / "config" / "watchlist.json").write_text('[{"name": "x"}]', encoding="utf-8")   # 名前が短すぎる
        sent = []
        with mock.patch.object(publish.shutil, "which", lambda n: "gh"), \
                mock.patch.object(publish.subprocess, "run", lambda *a, **k: sent.append(a) or mock.Mock(returncode=0, stdout="", stderr="")):
            with self.assertRaises(ValueError):
                publish.sync_watchlist(Paths(root))
        self.assertEqual(sent, [])

    def test_sends_valid_content_through_stdin_not_arguments(self):
        root = Path(tempfile.mkdtemp(prefix="stocknews_sync_"))
        (root / "config").mkdir()
        (root / "config" / "watchlist.json").write_text('[{"name": "テスト銘柄", "code": "1234"}]', encoding="utf-8")
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"], seen["input"] = cmd, kw.get("input")
            return mock.Mock(returncode=0, stdout="✓ Set secret", stderr="")

        with mock.patch.object(publish.shutil, "which", lambda n: "gh"), mock.patch.object(publish.subprocess, "run", fake_run):
            self.assertEqual(publish.sync_watchlist(Paths(root)), 0)
        self.assertNotIn("テスト銘柄", " ".join(seen["cmd"]))     # コマンドの引数（プロセス一覧に見える）には載せない
        self.assertIn("テスト銘柄", seen["input"])


if __name__ == "__main__":
    unittest.main()
