"""登录抓取、401 重登、并发合并与跨源保护测试。

使用真实的本地 HTTP 服务（werkzeug）启动 ``app.demo_site``，以便完整覆盖
Cookie、CSRF、401、延迟旧 401 与跨源重定向。
"""

import json
import threading
import time
import unittest
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup
from flask import Flask, request
from werkzeug.serving import make_server

from app import demo_site
from app.auth_fetch import (
    AuthConfig,
    AuthError,
    AuthManager,
    FetchError,
    auth_config_from_env,
    fetch_url,
    looks_like_login_page,
    origin_of,
)


class LiveServer:
    """在后台线程运行一个 Flask 应用。"""

    def __init__(self, flask_app, host="127.0.0.1"):
        self.server = make_server(host, 0, flask_app, threaded=True)
        self.port = self.server.server_port
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.thread.join(timeout=5)


class AuthFetchTests(unittest.TestCase):
    def setUp(self):
        demo_site.reset_state()
        demo_site.SESSION_TTL = 0
        # 绑定 0.0.0.0 便于用 127.0.0.2 验证“主机不同则不携带 Cookie”
        self.site = LiveServer(demo_site.app, host="0.0.0.0")
        self.addCleanup(self.site.close)
        self.config = AuthConfig(
            login_url=self.site.base + "/login",
            username=demo_site.DEMO_USERNAME,
            password=demo_site.DEMO_PASSWORD,
        )
        self.manager = AuthManager(self.config)

    @property
    def daily(self):
        return self.site.base + "/daily"

    # -- 基础登录 -----------------------------------------------------------
    def test_login_and_fetch_authenticated_daily(self):
        html = self.manager.fetch_authenticated(self.daily)
        self.assertIn("水库日报", html)
        self.assertIn("daily-table", html)
        self.assertFalse(looks_like_login_page(html))
        self.assertEqual(self.manager.login_count, 1)

    def test_wrong_password_raises_auth_error(self):
        bad = AuthManager(
            AuthConfig(
                login_url=self.config.login_url,
                username=demo_site.DEMO_USERNAME,
                password="wrong-password",
            )
        )
        with self.assertRaises(AuthError) as ctx:
            bad.fetch_authenticated(self.daily)
        self.assertIn("认证失败", str(ctx.exception))
        self.assertEqual(bad.login_count, 0)

    # -- 401 自动重登 -------------------------------------------------------
    def test_expired_session_relogins_and_retries_once(self):
        self.manager.fetch_authenticated(self.daily)
        requests.get(self.site.base + "/expire", timeout=5).raise_for_status()

        html = self.manager.fetch_authenticated(self.daily)
        self.assertIn("daily-table", html)
        self.assertEqual(self.manager.login_count, 2)

    def test_always_401_stops_after_exactly_one_retry(self):
        self.manager.get_session()
        before = self.manager.login_count
        with self.assertRaises(AuthError) as ctx:
            self.manager.fetch_authenticated(self.site.base + "/always-401")
        self.assertIn("不再继续重试", str(ctx.exception))
        # 只发生一次自动重登（首次 401 → 重登 → 重试仍 401 → 停止）
        self.assertEqual(self.manager.login_count, before + 1)

    # -- 并发合并重登 + 迟到的旧 401 ---------------------------------------
    def test_concurrent_expired_session_coalesces_and_late_401_reuses_new(self):
        self.manager.get_session()
        baseline = self.manager.login_count
        old_session = self.manager.get_session()
        requests.get(self.site.base + "/expire", timeout=5).raise_for_status()

        attempts = 4
        barrier = threading.Barrier(attempts)
        results = {}
        errors = []
        lock = threading.Lock()

        def worker(index):
            started = time.time()
            url = self.daily
            if index == 0:
                url += "?delay_401=0.9"
            try:
                barrier.wait(timeout=10)
                html = self.manager.fetch_authenticated(
                    url, session=old_session
                )
                with lock:
                    results[index] = (time.time() - started, html)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(f"{index}: {exc}")

        threads = [
            threading.Thread(target=worker, args=(i,), daemon=True)
            for i in range(attempts)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertFalse(errors, errors)
        self.assertEqual(len(results), attempts)
        # 4 个线程携带同一旧会话，只合并重登 1 次
        self.assertEqual(
            self.manager.login_count, baseline + 1,
            "并发 401 应合并为一次重登",
        )
        self.assertEqual(self.manager.generation, old_session.generation + 1)
        # 延迟线程确实经历了慢速旧 401，但最终成功
        self.assertGreaterEqual(results[0][0], 0.85)
        self.assertEqual(len({html for _, html in results.values()}), 1)
        self.assertIn("daily-table", results[0][1])
        # 迟到的旧 401 没有再次登录、也没有让新会话失效
        fresh_html = self.manager.fetch_authenticated(self.daily)
        self.assertIn("daily-table", fresh_html)

    # -- 站点只保留最近一次登录会话 ----------------------------------------
    def test_demo_site_keeps_only_latest_session(self):
        def login_session():
            sess = requests.Session()
            page = sess.get(self.site.base + "/login", timeout=5)
            soup = BeautifulSoup(page.text, "html.parser")
            csrf = soup.find("input", {"name": "csrf_token"})["value"]
            resp = sess.post(
                self.site.base + "/login",
                data={
                    "username": demo_site.DEMO_USERNAME,
                    "password": demo_site.DEMO_PASSWORD,
                    "csrf_token": csrf,
                },
                timeout=5,
            )
            self.assertEqual(resp.status_code, 200)
            self.assertIn("daily-table", resp.text)
            return sess

        first = login_session()
        second = login_session()
        self.assertEqual(first.get(self.site.base + "/daily").status_code, 401)
        self.assertEqual(second.get(self.site.base + "/daily").status_code, 200)

    # -- 跨源保护 -----------------------------------------------------------
    def test_cross_origin_redirect_does_not_carry_cookie_or_credentials(self):
        captured_app = Flask("capture")
        seen = []

        @captured_app.get("/capture")
        def capture():
            seen.append(dict(request.headers))
            return "<html><body>跨源捕获页面</body></html>"

        captured = LiveServer(captured_app)
        self.addCleanup(captured.close)

        self.manager.fetch_authenticated(self.daily)
        target = (
            self.site.base
            + "/redirect?to="
            + quote(captured.base + "/capture", safe="")
        )
        html = self.manager.fetch_authenticated(target)
        self.assertIn("跨源捕获页面", html)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].get("Cookie", ""), "")
        self.assertEqual(seen[0].get("Authorization", ""), "")

    def test_cross_origin_401_does_not_trigger_relogin(self):
        cross_app = Flask("cross-401")

        @cross_app.get("/unauthorized")
        def unauthorized():
            return "no", 401

        cross = LiveServer(cross_app)
        self.addCleanup(cross.close)

        self.manager.fetch_authenticated(self.daily)
        before = self.manager.login_count
        target = (
            self.site.base
            + "/redirect?to="
            + quote(cross.base + "/unauthorized", safe="")
        )
        with self.assertRaises(FetchError):
            self.manager.fetch_authenticated(target)
        self.assertEqual(self.manager.login_count, before)

    def test_host_mismatch_uses_anonymous_fetch_without_cookie(self):
        self.manager.fetch_authenticated(self.daily)
        # 同一监听端口，但主机名不同 → 不同源
        mismatched = f"http://127.0.0.2:{self.site.port}/echo"
        html = fetch_url(mismatched, manager=self.manager)
        data = json.loads(html)
        self.assertEqual(data["cookie"], "")
        self.assertEqual(data["authorization"], "")

        with self.assertRaises(AuthError):
            self.manager.fetch_authenticated(mismatched)

    def test_cross_origin_login_form_action_refuses_to_send_credentials(self):
        stolen_app = Flask("steal")
        stolen = []

        @stolen_app.post("/steal")
        def steal():
            stolen.append(request.form.to_dict())
            return "ok"

        stolen_server = LiveServer(stolen_app)
        self.addCleanup(stolen_server.close)

        cross_app = Flask("cross-login")

        @cross_app.get("/login")
        def cross_login():
            return (
                "<html><body>"
                '<form method="post" action="'
                f"http://127.0.0.1:{stolen_server.port}/steal\">"
                '<input type="hidden" name="csrf_token" value="abc123">'
                '<input type="password" name="password">'
                "</form></body></html>"
            )

        cross_server = LiveServer(cross_app)
        self.addCleanup(cross_server.close)

        manager = AuthManager(
            AuthConfig(
                login_url=cross_server.base + "/login",
                username=demo_site.DEMO_USERNAME,
                password=demo_site.DEMO_PASSWORD,
            )
        )
        with self.assertRaises(AuthError) as ctx:
            manager.get_session()
        self.assertIn("配置源之外", str(ctx.exception))
        self.assertEqual(stolen, [], "凭据绝不能 POST 到跨源 action")

    # -- 源匹配与环境配置 ---------------------------------------------------
    def test_origin_matching_normalizes_default_ports(self):
        self.assertEqual(origin_of("http://a.test/x"), ("http", "a.test", 80))
        self.assertEqual(origin_of("http://a.test:80/x"), ("http", "a.test", 80))
        self.assertEqual(
            origin_of("https://a.test/x"), ("https", "a.test", 443)
        )
        self.assertNotEqual(
            origin_of("https://a.test/x"), origin_of("http://a.test/x")
        )
        self.assertNotEqual(
            origin_of("http://a.test:8080/x"), origin_of("http://a.test:80/x")
        )
        self.assertNotEqual(
            origin_of("http://127.0.0.1/x"), origin_of("http://127.0.0.2/x")
        )

    def test_auth_config_from_env(self):
        config = auth_config_from_env(
            {
                "HYDRO_LOGIN_URL": "http://example.test/login",
                "HYDRO_USERNAME": "u",
                "HYDRO_PASSWORD": "p",
            }
        )
        self.assertIsNotNone(config)
        self.assertEqual(config.login_url, "http://example.test/login")
        self.assertEqual(config.csrf_field, "csrf_token")

        self.assertIsNone(auth_config_from_env({}))
        with self.assertRaises(ValueError):
            auth_config_from_env({"HYDRO_LOGIN_URL": "http://example.test/login"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
