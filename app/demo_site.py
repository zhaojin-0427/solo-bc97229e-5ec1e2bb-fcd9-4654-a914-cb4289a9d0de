"""本地日报演示站：表单登录、单会话、401 与“延迟旧 401”。

用途
====
供 Docker Compose / 本地开发时演示采集系统的登录能力：

* ``GET /login``：返回带 CSRF 隐藏字段的登录表单；
* ``POST /login``：校验账号、密码、CSRF，成功后**只保留最近一次会话**；
* ``GET /daily``：需要有效会话，否则返回 **401**；支持 ``?delay_401=秒``，
  让使用旧会话的请求延迟返回 401，用于复现“迟到的旧 401”并发场景；
* ``GET /expire``：立即让当前会话失效；
* ``GET /echo``：回显请求头（用于验证跨源不携带 Cookie）；
* ``GET /redirect?to=URL``：跳转到任意地址（演示跨源重定向不携带 Cookie）；
* ``GET /stats``：登录次数、会话世代等演示统计。

该站点仅用于本地演示，不应部署到生产环境。
"""

from __future__ import annotations

import os
import secrets
import threading
import time

from flask import Flask, Response, abort, jsonify, redirect, render_template_string, request

app = Flask(__name__)

DEMO_USERNAME = os.environ.get("DEMO_USERNAME", "hydro")
DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD", "hydro-demo-pass")

SESSION_COOKIE = "HYDRO_DEMO_SESSION"
CSRF_COOKIE = "HYDRO_DEMO_CSRF"

# 0 表示不按时间自动失效（仍可通过 /expire 手动失效）。
SESSION_TTL = float(os.environ.get("DEMO_SESSION_TTL_SECONDS", "0") or 0)

_lock = threading.RLock()
_sessions: dict[str, float] = {}
_csrf_tokens: dict[str, float] = {}
_latest_token: str | None = None
_login_count = 0


def reset_state() -> None:
    """重置演示站状态（测试与演示复位使用）。"""
    global _sessions, _csrf_tokens, _latest_token, _login_count
    with _lock:
        _sessions = {}
        _csrf_tokens = {}
        _latest_token = None
        _login_count = 0

def _extra_headers(resp: Response) -> Response:
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _new_csrf() -> str:
    token = secrets.token_urlsafe(24)
    with _lock:
        _csrf_tokens[token] = time.time()
        if len(_csrf_tokens) > 200:
            cutoff = time.time() - 3600
            for old in [k for k, ts in _csrf_tokens.items() if ts < cutoff]:
                _csrf_tokens.pop(old, None)
    return token


def _valid_session(token: str | None) -> bool:
    if not token:
        return False
    with _lock:
        created = _sessions.get(token)
        if created is None:
            return False
        # 站点只保留最近一次登录会话：旧 token 一律无效
        if token != _latest_token:
            return False
        if SESSION_TTL > 0 and time.time() - created > SESSION_TTL:
            return False
        return True


_LOGIN_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>水库日报站登录</title>
  <style>
    body { font-family: sans-serif; max-width: 720px; margin: 40px auto; color: #222; }
    h1 { color: #0f4c81; }
    .err { color: #b03a2e; background: #fdecea; padding: 10px 12px; border-radius: 6px; }
    form { display: grid; gap: 10px; max-width: 320px; margin-top: 16px; }
    input { padding: 9px 10px; }
    button { padding: 9px 14px; background: #1668a8; color: #fff; border: 0; border-radius: 6px; }
    table { border-collapse: collapse; margin-top: 24px; font-size: 13px; color: #555; }
    td { border: 1px solid #ccd; padding: 5px 9px; }
  </style>
</head>
<body>
  <h1>水库日报站登录</h1>
  <p>本页为本地演示站登录页，登录后仅可查看最新一期日报。</p>
  {% if error %}<p class="err">{{ error }}</p>{% endif %}
  <form method="post" action="/login">
    <input type="hidden" name="csrf_token" value="{{ csrf }}">
    <label>账号 <input type="text" name="username" autocomplete="username" required></label>
    <label>密码 <input type="password" name="password" autocomplete="current-password" required></label>
    <button type="submit">登录</button>
  </form>
  <!-- 登录说明表：用于验证登录页绝不会进入公示页缓存 -->
  <table id="login-help">
    <tr><td>登录说明</td><td>请使用部署环境变量中的账号密码</td></tr>
  </table>
</body>
</html>
"""

_DAILY_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>水库日报（{{ date }}）</title>
  <style>
    body { font-family: "PingFang SC", "Microsoft YaHei", sans-serif;
      max-width: 960px; margin: 30px auto; color: #222; padding: 0 16px; }
    h1 { font-size: 22px; text-align: center; }
    .meta { text-align: center; color: #666; font-size: 13px; margin-bottom: 16px; }
    table { border-collapse: collapse; width: 100%; font-size: 14px; }
    caption { font-weight: 700; margin-bottom: 8px; }
    th, td { border: 1px solid #555; padding: 7px 9px; text-align: center; }
  </style>
</head>
<body>
  <h1>某市大中型水库日报（{{ date }}）</h1>
  <p class="meta">登录后可见 · 本地日报演示站 · 仅保留最近一次登录会话</p>
  <table id="daily-table">
    <caption>表1 水库水情日报（两级表头）</caption>
    <tr>
      <th rowspan="2">水库名称</th>
      <th colspan="2">水位（米）</th>
      <th colspan="2">入库流量（立方米每秒）</th>
      <th rowspan="2">备注</th>
    </tr>
    <tr>
      <th>旬初</th><th>旬末</th><th>旬初</th><th>旬末</th>
    </tr>
    <tr>
      <td rowspan="2">青山水库</td>
      <td>128.40</td><td>129.05</td><td>86.20</td><td>89.74</td><td>正常</td>
    </tr>
    <tr>
      <td>129.05</td><td>129.62</td><td>41.30</td><td></td><td>最大流量缺测</td>
    </tr>
    <tr>
      <td>碧河水库</td>
      <td>76.10</td><td>75.88</td><td>42.05</td><td>43.10</td><td>供水期消落</td>
    </tr>
    <tr>
      <td>云岭水库</td>
      <td>203.77</td><td>204.11</td><td>2.10</td>
    </tr>
  </table>
</body>
</html>
"""


@app.get("/login")
def login_form():
    csrf = _new_csrf()
    resp = Response(
        render_template_string(
            _LOGIN_TEMPLATE, csrf=csrf, error=None
        )
    )
    resp.set_cookie(
        CSRF_COOKIE,
        csrf,
        httponly=True,
        samesite="Lax",
        path="/",
    )
    return _extra_headers(resp)


def _login_response(error: str | None, status: int = 200) -> Response:
    csrf = _new_csrf()
    resp = Response(
        render_template_string(_LOGIN_TEMPLATE, csrf=csrf, error=error),
        status=status,
    )
    resp.set_cookie(CSRF_COOKIE, csrf, httponly=True, samesite="Lax", path="/")
    return _extra_headers(resp)


@app.post("/login")
def login_submit():
    global _latest_token, _login_count

    form_csrf = request.form.get("csrf_token", "")
    cookie_csrf = request.cookies.get(CSRF_COOKIE, "")
    with _lock:
        csrf_ok = (
            bool(form_csrf)
            and form_csrf == cookie_csrf
            and form_csrf in _csrf_tokens
        )
        _csrf_tokens.pop(form_csrf, None)

    if (
        not csrf_ok
        or request.form.get("username", "") != DEMO_USERNAME
        or request.form.get("password", "") != DEMO_PASSWORD
    ):
        return _login_response("账号、密码或 CSRF 校验失败。", status=401)

    token = secrets.token_urlsafe(32)
    with _lock:
        # 只保留最近一次登录会话：旧会话全部作废
        _sessions.clear()
        _sessions[token] = time.time()
        _latest_token = token
        _login_count += 1

    resp = redirect("/daily", code=303)
    resp.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="Lax",
        path="/",
    )
    return _extra_headers(resp)


@app.get("/daily")
def daily():
    token = request.cookies.get(SESSION_COOKIE)
    if not _valid_session(token):
        # 模拟“迟到的旧 401”：先睡一段时间再拒绝，期间其他请求可能已重登
        delay = request.args.get("delay_401", type=float) or 0
        if delay > 0:
            time.sleep(min(delay, 10))
        return _extra_headers(Response(
            "会话已失效，请重新登录。",
            status=401,
            mimetype="text/plain",
        ))
    return _extra_headers(
        Response(render_template_string(_DAILY_TEMPLATE, date="2026-11-12"))
    )


@app.route("/expire", methods=["GET", "POST"])
def expire():
    global _latest_token
    with _lock:
        _sessions.clear()
        _latest_token = None
    return jsonify({"expired": True})


@app.get("/echo")
def echo():
    return jsonify(
        {
            "host": request.host,
            "cookie": request.headers.get("Cookie", ""),
            "authorization": request.headers.get("Authorization", ""),
        }
    )


@app.get("/redirect")
def redirect_to():
    to = request.args.get("to", "")
    if not to.startswith(("http://", "https://")):
        abort(400, "to 必须是 http(s) 地址")
    return redirect(to, code=302)


@app.get("/stats")
def stats():
    with _lock:
        return jsonify(
            {
                "login_count": _login_count,
                "active_sessions": len(_sessions),
                "latest_set": _latest_token is not None,
                "session_ttl": SESSION_TTL,
            }
        )


@app.get("/always-401")
def always_401():
    """始终返回 401，用于验证“每次抓取最多重试一次”。"""
    return _extra_headers(Response("always 401", status=401, mimetype="text/plain"))


@app.get("/")
def index():
    return redirect("/login", code=302)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8081, debug=False)
