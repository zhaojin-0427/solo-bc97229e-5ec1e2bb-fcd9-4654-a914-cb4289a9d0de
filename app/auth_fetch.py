"""面向“部署配置的单个站点”的登录抓取支持。

设计要点
========
* 凭据只从环境变量读取；登录流程为 ``GET 登录页取 CSRF → POST 账号/密码/CSRF``。
* Cookie 与凭据只发送给配置源，按 **协议 + 主机 + 端口** 精确匹配；手动跟随
  重定向，跨源跳转绝不携带 Cookie，登录表单 action 跨源时拒绝提交凭据。
* 同一进程内只保留最近一次登录会话（不可变快照 + 世代号）。遇到 401 时：
  - 每次抓取最多自动重登并重试一次；
  - 并发抓取遇到同一过期会话时在同一把锁下合并重登（single-flight）；
  - 迟到的旧 401 发现世代号已前进时直接复用新会话，不会再登录一次而
    把新会话作废。
* 登录页永远不会被当作公示页返回，因而不会进入上层公示缓存。
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Mapping, Optional
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

DEFAULT_USER_AGENT = "HydroTableCollector/1.0 (+reservoir bulletin archiver)"


class FetchError(Exception):
    """抓取失败（网络、HTTP 状态等非认证问题）。"""


class AuthError(FetchError):
    """登录/会话认证失败。"""


# ---------------------------------------------------------------------------
# 源（scheme + host + port）匹配
# ---------------------------------------------------------------------------

def origin_of(url: str) -> tuple[str, str, Optional[int]]:
    """返回 URL 的规范化源：``(scheme, host, port)``。

    端口使用协议默认值补全（http→80，https→443），因此
    ``http://a`` 与 ``http://a:80`` 视为同源，而改协议、改主机或改端口
    都视为跨源。
    """
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    try:
        explicit_port = parts.port
    except ValueError:
        explicit_port = None
    if explicit_port is None and scheme in ("http", "https"):
        port: Optional[int] = 443 if scheme == "https" else 80
    else:
        port = explicit_port
    return (scheme, host, port)


def same_origin(a: str, b: str) -> bool:
    return origin_of(a) == origin_of(b)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AuthConfig:
    """一个登录站点的配置。"""

    login_url: str
    username: str
    password: str
    user_field: str = "username"
    password_field: str = "password"
    csrf_field: str = "csrf_token"
    timeout: float = 15.0
    user_agent: str = DEFAULT_USER_AGENT

    @property
    def origin(self) -> tuple[str, str, Optional[int]]:
        return origin_of(self.login_url)


def auth_config_from_env(environ: Optional[Mapping[str, str]] = None) -> Optional[AuthConfig]:
    """从环境变量构造配置；三项都未设置时返回 ``None``。

    只配置了其中一部分视为部署错误，抛 :class:`ValueError`。
    """
    env = os.environ if environ is None else environ
    login_url = (env.get("HYDRO_LOGIN_URL") or "").strip()
    username = env.get("HYDRO_USERNAME")
    password = env.get("HYDRO_PASSWORD")

    if not login_url and username is None and password is None:
        return None
    if not login_url or username is None or password is None:
        raise ValueError(
            "登录站点配置不完整：需要同时设置 HYDRO_LOGIN_URL、"
            "HYDRO_USERNAME、HYDRO_PASSWORD。"
        )
    if urlsplit(login_url).scheme not in ("http", "https"):
        raise ValueError("HYDRO_LOGIN_URL 必须是 http(s) 地址。")

    def _float_env(name: str, default: float) -> float:
        raw = (env.get(name) or "").strip()
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError as exc:
            raise ValueError(f"{name} 必须是数字。") from exc

    return AuthConfig(
        login_url=login_url,
        username=username,
        password=password,
        user_field=(env.get("HYDRO_USER_FIELD") or "username").strip() or "username",
        password_field=(env.get("HYDRO_PASSWORD_FIELD") or "password").strip() or "password",
        csrf_field=(env.get("HYDRO_CSRF_FIELD") or "csrf_token").strip() or "csrf_token",
        timeout=_float_env("HYDRO_HTTP_TIMEOUT", 15.0),
        user_agent=(env.get("HYDRO_USER_AGENT") or DEFAULT_USER_AGENT).strip()
        or DEFAULT_USER_AGENT,
    )


# ---------------------------------------------------------------------------
# 登录页解析
# ---------------------------------------------------------------------------

def looks_like_login_page(html: str) -> bool:
    """粗略判断页面是否为登录页（存在密码输入框）。"""
    soup = BeautifulSoup(html, "html.parser")
    return soup.find("input", attrs={"type": "password"}) is not None


def _find_login_form(
    html: str,
    base_url: str,
    config: AuthConfig,
) -> tuple[str, dict[str, str]]:
    """从登录页中取表单 action 与隐藏字段（含 CSRF）。"""
    soup = BeautifulSoup(html, "html.parser")
    form = None
    for candidate in soup.find_all("form"):
        if (
            candidate.find("input", attrs={"type": "password"})
            or candidate.find("input", attrs={"name": config.password_field})
        ):
            form = candidate
            break
    if form is None:
        raise AuthError("登录页未找到包含密码字段的登录表单，无法自动登录。")

    action = urljoin(base_url, (form.get("action") or "").strip() or base_url)
    fields: dict[str, str] = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        typ = (inp.get("type") or "text").lower()
        if typ in ("submit", "button", "reset", "file", "checkbox", "radio"):
            continue
        fields[name] = inp.get("value") or ""

    csrf_name = config.csrf_field
    csrf_value = fields.get(csrf_name, "")
    if not csrf_value:
        for name, value in fields.items():
            lowered = name.lower()
            if value and ("csrf" in lowered or "token" in lowered):
                csrf_name, csrf_value = name, value
                break
    if not csrf_value:
        # 少数站点把 token 放在 meta 标签中
        meta = soup.find(
            "meta",
            attrs={"name": lambda n: bool(n) and ("csrf" in n.lower() or "token" in n.lower())},
        )
        if meta is not None:
            csrf_value = meta.get("content") or ""
            csrf_name = meta.get("name") or config.csrf_field
    if not csrf_value:
        raise AuthError("登录页未找到 CSRF 令牌（hidden input 或 meta 标签）。")

    fields[csrf_name] = csrf_value
    return action, fields


def _response_text(resp: requests.Response) -> str:
    """按响应头/内容推断编码，兼容中文站点常见的 GB 系编码。"""
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


# ---------------------------------------------------------------------------
# 作用域内 Cookie 罐（只保存配置源的 Cookie）
# ---------------------------------------------------------------------------

class _CookieJar:
    def __init__(self, origin: tuple[str, str, Optional[int]]):
        self.origin = origin
        self.cookies: dict[str, str] = {}

    def update(self, url: str, resp: requests.Response) -> None:
        if origin_of(url) != self.origin:
            return
        for cookie in resp.cookies:
            if cookie.value is None or cookie.is_expired():
                self.cookies.pop(cookie.name, None)
            else:
                self.cookies[cookie.name] = cookie.value

    def header(self) -> str:
        return "; ".join(f"{name}={value}" for name, value in self.cookies.items())


# ---------------------------------------------------------------------------
# 会话快照与登录管理器
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SessionSnapshot:
    """一次成功登录后的不可变会话快照。"""

    generation: int
    cookie_header: str


class AuthManager:
    """单站点登录会话管理器；线程安全。

    只保留最近一次成功登录的会话。登录网络请求在锁内完成，因此并发发现
    会话过期时只有第一个线程真正重登，其余线程拿到新会话。
    """

    def __init__(self, config: AuthConfig):
        self.config = config
        self._lock = threading.RLock()
        self._current: Optional[SessionSnapshot] = None
        self._generation = 0
        self._login_count = 0

    # -- 属性 ---------------------------------------------------------------
    @property
    def origin(self) -> tuple[str, str, Optional[int]]:
        return self.config.origin

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def login_count(self) -> int:
        with self._lock:
            return self._login_count

    def matches_origin(self, url: str) -> bool:
        return origin_of(url) == self.origin

    # -- 会话获取/恢复 ------------------------------------------------------
    def get_session(self) -> SessionSnapshot:
        with self._lock:
            if self._current is not None:
                return self._current
            return self._login_locked()

    def recover(self, seen: SessionSnapshot) -> SessionSnapshot:
        """处理某次请求携带 ``seen`` 会话却收到 401 的情况。

        若当前会话世代已经前进（其他线程已重登成功），直接复用当前会话，
        **不再次登录**——这正是“迟到的旧 401 不能使新会话失效”的关键。
        """
        with self._lock:
            current = self._current
            if current is not None and current.generation != seen.generation:
                return current
            return self._login_locked()

    def _login_locked(self) -> SessionSnapshot:
        """在持锁状态下完成一次登录；失败不推进世代号。"""
        cookie_header = self._perform_login()
        self._generation += 1
        self._login_count += 1
        snapshot = SessionSnapshot(
            generation=self._generation,
            cookie_header=cookie_header,
        )
        self._current = snapshot
        return snapshot

    # -- HTTP 原语 ----------------------------------------------------------
    def _request(
        self,
        method: str,
        url: str,
        *,
        data: Optional[dict[str, str]] = None,
        cookie_header: Optional[str] = None,
    ) -> requests.Response:
        headers = {
            "User-Agent": self.config.user_agent,
            "Accept": "text/html,application/xhtml+xml",
        }
        # 只有目标与配置源完全同源才携带会话 Cookie；跨源/跨端口/跨协议
        # 一律不携带。
        if cookie_header and origin_of(url) == self.origin:
            headers["Cookie"] = cookie_header
        try:
            return requests.request(
                method,
                url,
                data=data,
                headers=headers,
                timeout=self.config.timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise FetchError(f"网络请求失败：{exc}") from exc

    def _follow_with_jar(
        self, resp: requests.Response, jar: _CookieJar, max_hops: int = 5
    ) -> requests.Response:
        """登录流程内手动跟随重定向，逐跳收集配置源 Cookie。"""
        for _ in range(max_hops):
            jar.update(resp.url, resp)
            if not resp.is_redirect:
                return resp
            if resp.status_code in (307, 308):
                # 不重放含凭据的 POST，避免跨源泄露或重复提交
                raise AuthError(
                    "登录流程返回 307/308 重定向，为避免重放凭据已停止登录。"
                )
            location = resp.headers.get("location")
            if not location:
                return resp
            next_url = urljoin(resp.url, location)
            resp = self._request("GET", next_url, cookie_header=jar.header())
        raise AuthError("登录流程重定向次数过多，已停止。")

    def _get_with_session(
        self, url: str, cookie_header: str, max_hops: int = 5
    ) -> requests.Response:
        """抓取公示页，手动跟随重定向；跨源跳转不携带 Cookie。"""
        resp = self._request("GET", url, cookie_header=cookie_header)
        for _ in range(max_hops):
            if not resp.is_redirect:
                return resp
            location = resp.headers.get("location")
            if not location:
                return resp
            next_url = urljoin(resp.url, location)
            resp = self._request("GET", next_url, cookie_header=cookie_header)
        raise FetchError("抓取页面重定向次数过多，已停止。")

    # -- 登录 ---------------------------------------------------------------
    def _perform_login(self) -> str:
        jar = _CookieJar(self.origin)

        resp = self._request("GET", self.config.login_url)
        resp = self._follow_with_jar(resp, jar)
        if resp.status_code >= 400:
            raise AuthError(f"认证失败：登录页返回 HTTP {resp.status_code}。")
        login_html = _response_text(resp)
        action, fields = _find_login_form(login_html, resp.url, self.config)

        # 凭据只允许提交到配置源；跨源 action 直接拒绝。
        if origin_of(action) != self.origin:
            raise AuthError(
                "认证失败：登录表单 action 指向配置源之外，"
                "已拒绝发送账号密码与 CSRF 令牌。"
            )

        data = dict(fields)
        data[self.config.user_field] = self.config.username
        data[self.config.password_field] = self.config.password

        resp = self._request(
            "POST",
            action,
            data=data,
            cookie_header=jar.header(),
        )
        resp = self._follow_with_jar(resp, jar)

        if resp.status_code in (401, 403):
            raise AuthError(
                "认证失败：账号、密码或 CSRF 校验未通过"
                f"（HTTP {resp.status_code}）。请检查环境变量中的凭据。"
            )
        if resp.status_code >= 400:
            raise AuthError(f"认证失败：登录响应返回 HTTP {resp.status_code}。")

        final_html = _response_text(resp)
        if looks_like_login_page(final_html):
            raise AuthError(
                "认证失败：登录后仍返回登录页，账号、密码或 CSRF 校验未通过。"
            )

        cookie_header = jar.header()
        if not cookie_header:
            raise AuthError("认证失败：登录成功响应没有设置任何会话 Cookie。")
        return cookie_header

    # -- 对外抓取 -----------------------------------------------------------
    def fetch_authenticated(
        self,
        url: str,
        session: Optional[SessionSnapshot] = None,
    ) -> str:
        """抓取配置源内页面；401 时自动重登并最多重试一次。

        :param session: 可选指定本次请求使用哪个会话快照（并发演示/测试用来
            固定使用同一个旧会话）。默认取当前最新会话。
        """
        if origin_of(url) != self.origin:
            raise AuthError("该 URL 不属于已配置的登录站点，拒绝携带会话 Cookie。")

        snapshot = session if session is not None else self.get_session()
        resp = self._get_with_session(url, snapshot.cookie_header)

        # 只有配置源自身返回的 401 才代表“本会话过期”。跨源重定向后的
        # 401 不属于本会话，不应触发重登。
        if resp.status_code == 401 and origin_of(resp.url) == self.origin:
            snapshot = self.recover(snapshot)
            resp = self._get_with_session(url, snapshot.cookie_header)
            if resp.status_code == 401 and origin_of(resp.url) == self.origin:
                raise AuthError(
                    "会话失效后已自动重登录一次，但重试仍返回 401；"
                    "本次抓取不再继续重试。"
                )

        if resp.status_code in (401, 403):
            if origin_of(resp.url) != self.origin:
                raise FetchError(
                    f"跨源页面返回 HTTP {resp.status_code}，不触发配置源重登。"
                )
            raise AuthError(
                f"认证失败：服务器返回 HTTP {resp.status_code}。"
                "请检查环境变量中的账号和密码。"
            )
        if resp.status_code >= 400:
            raise FetchError(f"抓取页面失败：HTTP {resp.status_code}。")

        html = _response_text(resp)
        if looks_like_login_page(html):
            raise AuthError(
                "认证失败：抓取结果仍是登录页；登录页不会进入公示缓存。"
            )
        return html


# ---------------------------------------------------------------------------
# 无认证抓取与统一入口
# ---------------------------------------------------------------------------

def anonymous_fetch(
    url: str,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout: float = 15.0,
) -> str:
    """匿名抓取任意页面；不使用任何登录会话或配置 Cookie。"""
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": user_agent},
            timeout=timeout,
            allow_redirects=True,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise FetchError(f"抓取页面失败：{exc}") from exc
    return _response_text(resp)


def fetch_url(
    url: str,
    manager: Optional[AuthManager] = None,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout: float = 15.0,
) -> str:
    """统一抓取入口：配置源走登录会话，其余地址匿名抓取。"""
    if manager is not None and manager.matches_origin(url):
        return manager.fetch_authenticated(url)
    return anonymous_fetch(url, user_agent=user_agent, timeout=timeout)
