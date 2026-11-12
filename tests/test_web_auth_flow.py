"""Web 层登录站点集成：抓取 → 选表 → 转换 → CSV，以及认证失败与并发演示。"""

import unittest
from unittest import mock

import requests
from werkzeug.serving import make_server
import threading

from app import demo_site
from app.auth_fetch import AuthConfig, AuthManager
from app.web import app, _FETCH_CACHE, _WebAuth


class LiveServer:
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


class WebAuthFlowTests(unittest.TestCase):
    def setUp(self):
        app.testing = True
        self.client = app.test_client()
        _FETCH_CACHE.clear()
        demo_site.reset_state()
        demo_site.SESSION_TTL = 0
        self.site = LiveServer(demo_site.app, host="0.0.0.0")
        self.addCleanup(self.site.close)
        self.daily = self.site.base + "/daily"
        self.expire = self.site.base + "/expire"

    def _web_auth(self, password=None):
        manager = AuthManager(
            AuthConfig(
                login_url=self.site.base + "/login",
                username=demo_site.DEMO_USERNAME,
                password=password or demo_site.DEMO_PASSWORD,
            )
        )
        return _WebAuth(
            manager=manager,
            target_url=self.daily,
            expire_url=self.expire,
        )

    def test_index_shows_configured_login_site(self):
        with mock.patch("app.web._AUTH", self._web_auth()):
            resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        body = resp.data.decode()
        self.assertIn("登录站点（部署配置）", body)
        self.assertIn("采集配置站点日报", body)
        self.assertIn("运行并发采集演示", body)
        # 不应把凭据内容渲染出来
        self.assertNotIn(demo_site.DEMO_PASSWORD, body)

    def test_full_flow_from_authenticated_site(self):
        with mock.patch("app.web._AUTH", self._web_auth()):
            resp = self.client.post("/fetch", data={"url": self.daily})
            self.assertEqual(resp.status_code, 200)
            body = resp.data.decode()
            self.assertIn("选择表格与表头行数", body)
            # 登录页里的说明表绝不能出现在公示表格选择页
            self.assertNotIn("登录说明", body)
            # 公示缓存里也只有日报页，没有登录页内容
            self.assertFalse(
                any(
                    "登录说明" in item.get("html", "")
                    for item in _FETCH_CACHE.values()
                )
            )
            key = body.split('name="cache_key" value="')[1].split('"')[0]

            resp = self.client.post(
                "/parse",
                data={"cache_key": key, "table_index": 0, "header_rows": 2},
            )
            self.assertEqual(resp.status_code, 200)
            self.assertIn("转换结果预览", resp.data.decode())
            result_key = resp.data.decode().split("/download?key=")[1].split('"')[0]

            resp = self.client.get(f"/download?key={result_key}")
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.data.startswith(b"\xef\xbb\xbf"))
            rows = resp.data.decode("utf-8-sig").splitlines()
            # 4 条正文行 × 5 个观测列 = 20 条记录 + 表头
            self.assertEqual(len(rows) - 1, 20)
            self.assertIn("水库,表头路径", rows[0])
            self.assertIn("青山水库", resp.data.decode("utf-8-sig"))

    def test_auth_failure_reports_on_page_and_never_caches_login_page(self):
        with mock.patch("app.web._AUTH", self._web_auth(password="wrong-pass")):
            resp = self.client.post("/fetch", data={"url": self.daily})
        self.assertEqual(resp.status_code, 401)
        body = resp.data.decode()
        self.assertIn("认证失败", body)
        # 登录页没有进入公示缓存
        self.assertEqual(_FETCH_CACHE, {})
        self.assertNotIn("登录说明", body)

    def test_fetching_login_page_itself_is_rejected_and_not_cached(self):
        with mock.patch("app.web._AUTH", self._web_auth()):
            resp = self.client.post(
                "/fetch", data={"url": self.site.base + "/login"}
            )
        self.assertEqual(resp.status_code, 401)
        self.assertIn("登录页", resp.data.decode())
        self.assertEqual(_FETCH_CACHE, {})

    def test_concurrent_web_fetch_coalesces_expired_session_relogin(self):
        auth = self._web_auth()
        manager = auth.manager
        with mock.patch("app.web._AUTH", auth):
            manager.get_session()
            baseline = manager.login_count
            requests.get(self.expire, timeout=5).raise_for_status()

            attempts = 4
            barrier = threading.Barrier(attempts)
            responses = {}
            errors = []
            lock = threading.Lock()

            def worker(index):
                url = self.daily
                if index == 0:
                    url += "?delay_401=0.9"
                try:
                    barrier.wait(timeout=10)
                    client = app.test_client()
                    resp = client.post("/fetch", data={"url": url})
                    with lock:
                        responses[index] = resp
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
        self.assertEqual(len(responses), attempts)
        self.assertTrue(all(r.status_code == 200 for r in responses.values()))
        # 4 个并发 /fetch 共用同一过期会话，只重登一次
        self.assertEqual(manager.login_count, baseline + 1)
        for resp in responses.values():
            body = resp.data.decode()
            self.assertIn("选择表格与表头行数", body)
            self.assertNotIn("登录说明", body)

        stats = requests.get(self.site.base + "/stats", timeout=5).json()
        self.assertEqual(stats["login_count"], baseline + 1)

        key = responses[0].data.decode().split(
            'name="cache_key" value="'
        )[1].split('"')[0]
        resp = self.client.post(
            "/parse",
            data={"cache_key": key, "table_index": 0, "header_rows": 2},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("转换结果预览", resp.data.decode())

    def test_concurrent_demo_logs_in_once_and_converts(self):
        with mock.patch("app.web._AUTH", self._web_auth()):
            resp = self.client.post("/demo/concurrent")
        self.assertEqual(resp.status_code, 200)
        body = resp.data.decode()
        self.assertIn("并发采集演示通过", body)
        self.assertIn("4/4", body)
        # 合并重登：只增加 1 次登录
        self.assertIn("+1", body)
        self.assertIn("转换结果预览", body)

        result_key = body.split("/download?key=")[1].split('"')[0]
        resp = self.client.get(f"/download?key={result_key}")
        self.assertEqual(resp.status_code, 200)
        rows = resp.data.decode("utf-8-sig").splitlines()
        self.assertEqual(len(rows) - 1, 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)
