"""Flask 采集入口：抓取公示页 → 选择表格/表头行数 → 预览 → 下载 CSV。"""

from __future__ import annotations

import csv
import io
import os
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Optional

import requests
from bs4 import BeautifulSoup
from flask import (
    Flask,
    Response,
    abort,
    render_template,
    request,
    send_from_directory,
    url_for,
)

from .auth_fetch import (
    AuthError,
    AuthManager,
    FetchError,
    auth_config_from_env,
    fetch_url,
    looks_like_login_page,
    origin_of,
)
from .grid_parser import (
    TableParseError,
    lenient_matrix,
    parse_table,
)

app = Flask(__name__)

# 演示用进程内缓存（单 worker 足够）：key -> {"html", "url", "tables"}
_FETCH_CACHE: dict[str, dict] = {}
_FETCH_TTL = 30 * 60  # 30 分钟
_FETCH_MAX = 50

SAMPLE_URL = "/sample/notice.html"
SAMPLE_ERROR_URL = "/sample/notice_error.html"
USER_AGENT = "HydroTableCollector/1.0 (+reservoir bulletin archiver)"


@dataclass
class _WebAuth:
    """Web 层可见的登录站点信息（不含凭据明文）。"""

    manager: AuthManager
    target_url: str = ""
    expire_url: str = ""

    @property
    def origin_label(self) -> str:
        scheme, host, port = self.manager.origin
        default = 443 if scheme == "https" else 80
        if port == default:
            return f"{scheme}://{host}"
        return f"{scheme}://{host}:{port}"


def _load_web_auth(environ=None) -> Optional[_WebAuth]:
    """按环境变量装配登录站点；未配置或配置无效时返回 None。"""
    source = os.environ if environ is None else environ
    try:
        config = auth_config_from_env(source)
    except ValueError as exc:
        app.logger.warning("登录站点配置无效，已禁用自动登录：%s", exc)
        return None
    if config is None:
        return None

    target_url = (source.get("HYDRO_AUTH_URL") or "").strip()
    expire_url = (source.get("HYDRO_EXPIRE_URL") or "").strip()
    if target_url and origin_of(target_url) != config.origin:
        app.logger.warning(
            "HYDRO_AUTH_URL 与登录站点不同源，已忽略该演示入口。"
        )
        target_url = ""
    if expire_url and origin_of(expire_url) != config.origin:
        app.logger.warning(
            "HYDRO_EXPIRE_URL 与登录站点不同源，已忽略该演示入口。"
        )
        expire_url = ""
    return _WebAuth(
        manager=AuthManager(config),
        target_url=target_url,
        expire_url=expire_url,
    )


_AUTH: Optional[_WebAuth] = _load_web_auth()


def _cache_put(key: str, payload: dict) -> None:
    now = time.time()
    expired = [k for k, v in _FETCH_CACHE.items() if now - v["ts"] > _FETCH_TTL]
    for k in expired:
        _FETCH_CACHE.pop(k, None)
    while len(_FETCH_CACHE) >= _FETCH_MAX:
        _FETCH_CACHE.pop(next(iter(_FETCH_CACHE)))
    payload["ts"] = now
    _FETCH_CACHE[key] = payload


def _cache_get(key: str) -> Optional[dict]:
    item = _FETCH_CACHE.get(key)
    if item is None:
        return None
    if time.time() - item["ts"] > _FETCH_TTL:
        _FETCH_CACHE.pop(key, None)
        return None
    return item


def _is_local_sample(url: str) -> bool:
    return url.startswith("/sample/")


def _normalize_url(raw: str) -> str:
    url = (raw or "").strip()
    if not url:
        raise TableParseError("请输入公示页 URL。")
    if url.startswith(("http://", "https://", "/")):
        return url
    return "https://" + url


def _fetch_html(url: str) -> str:
    if _is_local_sample(url):
        # 本地示例：直接读静态文件，保证离线可演示
        path = url[len("/sample/"):]
        safe = path.replace("/", "_")
        full = app.static_folder + "/samples/" + safe
        try:
            with open(full, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError as exc:
            raise TableParseError(f"本地示例页不存在：{url}（{exc}）")

    try:
        html = fetch_url(
            url,
            manager=_AUTH.manager if _AUTH is not None else None,
            user_agent=USER_AGENT,
            timeout=15,
        )
    except AuthError:
        # 认证失败向上传递，由路由渲染错误页——登录页本身绝不写入缓存
        raise
    except FetchError as exc:
        raise TableParseError(str(exc)) from exc

    # 防御性检查：即便配置源返回了登录页，也按认证失败处理，不进入缓存。
    if (
        _AUTH is not None
        and origin_of(url) == _AUTH.manager.origin
        and looks_like_login_page(html)
    ):
        raise AuthError(
            "认证失败：抓取结果仍是登录页；登录页不会进入公示缓存。"
        )
    return html


def _parse_tables(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    summaries = []
    for idx, table in enumerate(soup.find_all("table")):
        rows = table.find_all("tr")
        cell_count = len(table.find_all(["td", "th"]))
        caption = table.find("caption")
        caption_text = caption.get_text(strip=True) if caption else ""
        # 容错预览：默认按 2 行表头展开
        grid, width = lenient_matrix(table, header_rows=min(2, max(1, len(rows))))
        preview = []
        for line in grid[: 6]:
            preview.append([p.text if p else "" for p in line[: 8]])
        summaries.append(
            {
                "index": idx,
                "n_rows": len(rows),
                "n_cells": cell_count,
                "width": width,
                "caption": caption_text,
                "preview": preview,
            }
        )
    return summaries


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return render_template(
        "index.html",
        sample_url=SAMPLE_URL,
        sample_error_url=SAMPLE_ERROR_URL,
        auth_configured=_AUTH is not None,
        auth_target_url=_AUTH.target_url if _AUTH is not None else "",
        auth_origin_label=_AUTH.origin_label if _AUTH is not None else "",
        auth_demo_ready=bool(
            _AUTH is not None and _AUTH.target_url and _AUTH.expire_url
        ),
    )


@app.post("/fetch")
def fetch():
    raw_url = request.form.get("url", "")
    try:
        url = _normalize_url(raw_url)
        html = _fetch_html(url)
    except AuthError as exc:
        return render_template(
            "error.html", message=str(exc), stage="认证"
        ), 401
    except TableParseError as exc:
        return render_template("error.html", message=str(exc), stage="抓取"), 400

    soup = BeautifulSoup(html, "html.parser")
    tables_html = soup.find_all("table")
    if not tables_html:
        return render_template(
            "error.html",
            message="该页面没有解析到任何 <table> 表格，请确认 URL 指向公示页。",
            stage="解析",
        ), 400

    summaries = _parse_tables(html)
    cache_key = "f" + secrets.token_urlsafe(16)
    _cache_put(
        cache_key,
        {"html": html, "url": url, "tables": summaries},
    )
    return render_template(
        "select.html",
        cache_key=cache_key,
        url=url,
        tables=summaries,
    )


@app.route("/demo/concurrent", methods=["GET", "POST"])
def demo_concurrent():
    """演示：并发抓取同一过期会话 → 合并重登 + 迟到旧 401 复用新会话 → 转换。"""
    if _AUTH is None or not _AUTH.target_url:
        return render_template(
            "error.html",
            message="未配置登录站点。请设置 HYDRO_LOGIN_URL、HYDRO_USERNAME、"
            "HYDRO_PASSWORD 与 HYDRO_AUTH_URL。",
            stage="演示",
        ), 400
    if not _AUTH.expire_url:
        return render_template(
            "error.html",
            message="未配置会话失效入口 HYDRO_EXPIRE_URL，无法运行并发演示。",
            stage="演示",
        ), 400

    manager = _AUTH.manager
    target_url = _AUTH.target_url

    try:
        # 先确保有一个会话，并把服务端会话置为失效状态
        manager.get_session()
        baseline_logins = manager.login_count
        requests.get(
            _AUTH.expire_url,
            headers={"User-Agent": USER_AGENT},
            timeout=5,
        ).raise_for_status()
    except (AuthError, FetchError, requests.RequestException) as exc:
        return render_template(
            "error.html",
            message=f"并发演示准备失败：{exc}",
            stage="演示",
        ), 502

    old_session = manager.get_session()
    attempts = 4
    barrier = threading.Barrier(attempts)
    results: list[tuple[int, str]] = []
    failures: list[str] = []
    result_lock = threading.Lock()
    started = time.time()

    def _worker(index: int) -> None:
        # 第一个请求故意让服务端延迟返回旧会话的 401，制造“迟到的旧 401”
        request_url = target_url
        if index == 0:
            sep = "&" if "?" in target_url else "?"
            request_url = f"{target_url}{sep}delay_401=1.2"
        try:
            barrier.wait(timeout=10)
            html = manager.fetch_authenticated(
                request_url, session=old_session
            )
            with result_lock:
                results.append((index, html))
        except Exception as exc:  # noqa: BLE001 - 演示页需要展示失败原因
            with result_lock:
                failures.append(f"线程 {index}: {exc}")

    workers = [
        threading.Thread(target=_worker, args=(i,), daemon=True)
        for i in range(attempts)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)
    alive = [w for w in workers if w.is_alive()]

    elapsed_ms = int((time.time() - started) * 1000)
    login_delta = manager.login_count - baseline_logins

    if failures or alive or len(results) != attempts:
        message = "；".join(failures) or "并发线程未在超时内完成。"
        return render_template(
            "error.html",
            message=f"并发采集未能全部成功：{message}",
            stage="演示",
        ), 502

    html = results[0][1]
    distinct_pages = len({page for _, page in results})
    tables = BeautifulSoup(html, "html.parser").find_all("table")
    if not tables:
        return render_template(
            "error.html",
            message="并发采集成功，但日报页中没有解析到表格。",
            stage="演示",
        ), 400

    try:
        parsed = parse_table(tables[0], 2, source_url=target_url)
    except TableParseError as exc:
        return render_template(
            "error.html", message=str(exc), stage="转换", stopped=True
        ), 422

    result_key = "r" + secrets.token_urlsafe(16)
    _cache_put(
        result_key,
        {
            "records": parsed.records,
            "url": target_url,
            "header_paths": parsed.header_paths,
            "width": parsed.width,
            "total_rows": parsed.total_rows,
            "header_rows": parsed.header_rows,
        },
    )

    concurrency = {
        "attempts": attempts,
        "successes": len(results),
        "failures": len(failures),
        "login_delta": login_delta,
        "generation": manager.generation,
        "old_generation": old_session.generation,
        "elapsed_ms": elapsed_ms,
        "distinct_pages": distinct_pages,
        "delayed_worker_ok": any(i == 0 for i, _ in results),
    }
    return render_template(
        "preview.html",
        result_key=result_key,
        url=target_url,
        parsed=parsed,
        records=parsed.records,
        concurrency=concurrency,
    )


@app.post("/parse")
def parse():
    cache_key = request.form.get("cache_key", "")
    cached = _cache_get(cache_key)
    if cached is None:
        return render_template(
            "error.html",
            message="会话已过期或缺失，请重新输入 URL 抓取。",
            stage="选择",
        ), 400

    try:
        table_index = int(request.form.get("table_index", ""))
        header_rows = int(request.form.get("header_rows", ""))
    except (TypeError, ValueError):
        return render_template(
            "error.html", message="表格编号或表头行数无效。", stage="选择"
        ), 400

    soup = BeautifulSoup(cached["html"], "html.parser")
    tables = soup.find_all("table")
    if not 0 <= table_index < len(tables):
        abort(400, "表格编号超出范围。")

    try:
        parsed = parse_table(
            tables[table_index], header_rows, source_url=cached["url"]
        )
    except TableParseError as exc:
        return render_template(
            "error.html", message=str(exc), stage="转换", stopped=True
        ), 422

    result_key = "r" + secrets.token_urlsafe(16)
    _cache_put(
        result_key,
        {
            "records": parsed.records,
            "url": cached["url"],
            "header_paths": parsed.header_paths,
            "width": parsed.width,
            "total_rows": parsed.total_rows,
            "header_rows": parsed.header_rows,
        },
    )
    return render_template(
        "preview.html",
        result_key=result_key,
        url=cached["url"],
        parsed=parsed,
        records=parsed.records,
    )


@app.get("/download")
def download():
    result_key = request.args.get("key", "")
    cached = _cache_get(result_key)
    if cached is None:
        return render_template(
            "error.html",
            message="下载会话已过期，请重新抓取并转换。",
            stage="下载",
        ), 400

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "水库",
            "表头路径",
            "原始文本",
            "来源单元格行",
            "来源单元格列",
            "来源单元格坐标",
            "来源URL",
        ]
    )
    for rec in cached["records"]:
        writer.writerow(
            [
                rec["reservoir"],
                rec["header_path"],
                rec["value"],
                rec["source_row"],
                rec["source_col"],
                rec["source_cell"],
                rec["source_url"],
            ]
        )

    # utf-8-sig 便于 Excel 直接打开中文
    payload = buf.getvalue().encode("utf-8-sig")
    fname = "reservoir_observations.csv"
    quoted = urllib.parse.quote(fname)
    return Response(
        payload,
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition":
                f"attachment; filename={fname}; filename*=UTF-8''{quoted}"
        },
    )


@app.get("/sample/<path:filename>")
def sample(filename: str):
    return send_from_directory(
        app.static_folder + "/samples", filename, mimetype="text/html"
    )


@app.errorhandler(404)
def not_found(_err):
    return render_template("error.html", message="页面不存在。", stage="路由"), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
