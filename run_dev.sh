#!/usr/bin/env bash
# 本地开发启动（无 Docker 环境时使用）。
# 依赖：pip install -r requirements.txt
# 同时启动本地日报演示站（8081）与采集 Web（8000）。
set -euo pipefail
cd "$(dirname "$0")"

export PYTHONUNBUFFERED=1

# 演示站凭据与采集端登录配置（均可用同名环境变量覆盖）
export DEMO_USERNAME="${DEMO_USERNAME:-hydro}"
export DEMO_PASSWORD="${DEMO_PASSWORD:-hydro-demo-pass}"
export HYDRO_LOGIN_URL="${HYDRO_LOGIN_URL:-http://127.0.0.1:8081/login}"
export HYDRO_AUTH_URL="${HYDRO_AUTH_URL:-http://127.0.0.1:8081/daily}"
export HYDRO_EXPIRE_URL="${HYDRO_EXPIRE_URL:-http://127.0.0.1:8081/expire}"
export HYDRO_USERNAME="${HYDRO_USERNAME:-$DEMO_USERNAME}"
export HYDRO_PASSWORD="${HYDRO_PASSWORD:-$DEMO_PASSWORD}"

python3 -m gunicorn \
  --bind 127.0.0.1:8081 \
  --workers 1 --threads 8 --timeout 60 \
  app.demo_site:app &
SITE_PID=$!
cleanup() {
  kill "$SITE_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# 等待演示站就绪后再启动采集 Web
python3 - <<'PY'
import time, urllib.request
for _ in range(60):
    try:
        urllib.request.urlopen("http://127.0.0.1:8081/login", timeout=1)
        break
    except Exception:
        time.sleep(0.2)
PY

python3 -m gunicorn \
  --bind 0.0.0.0:8000 \
  --workers 1 --threads 8 --timeout 60 \
  --reload \
  app.web:app
