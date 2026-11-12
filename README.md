# 水库公示表格采集系统（Hydro Table Collector）

把水库公示网页中的**合并单元格 HTML 表格**转换为可入库的**结构化观测记录**，
提供从 URL 抓取、表单登录、表格选择、表头指定、规则校验、预览到 CSV 下载的完整 Web 流程。

## 功能与转换规则

1. **采集网页（Flask）**：输入公示页 URL，抓取并列出页面内全部 HTML 表格，
   逐个给出容错迷你预览，选择目标表格并指定**顶部 1～3 行表头**。
2. **首列固定为水库名**：正文仅首列允许 `rowspan`，跨度内向下继承水库名；
   其余正文单元格不允许任何合并。
3. **按 `rowspan` / `colspan` 展开为规则网格**：
   - 表宽 = **首行展开宽度**（首行各单元格 colspan 之和）；
   - 同一来源单元格只记录一次，表头路径不重复同一段；
   - 路径文本相同的不同列仍分别保留，不合并。
4. **完整表头路径**：沿表头各行向下拼接，分隔符为 ` / `
   （如 `水位（米） / 旬初`）。
5. **空值语义**：观测空单元格保持为空；短行缺失的网格格位同样导出空值，
   且不携带来源单元格坐标。
6. **结构错误即停**：跨度重叠、横向越界、跨越表头/正文、正文非法合并时，
   标明来源单元格位置（`R行C列`，按**原表行序及行内单元格序从 1 计数**）
   并停止转换，不产生任何记录。
7. **CSV 导出**：字段为 `水库, 表头路径, 原始文本, 来源单元格行,
   来源单元格列, 来源单元格坐标, 来源URL`，UTF-8 带 BOM，Excel 可直接打开。

## 登录站点采集（新增）

部署时可为一个站点启用表单登录。登录流程：

```
GET 登录页 → 解析 CSRF 隐藏字段 → POST 账号/密码/CSRF → 获取会话 Cookie
```

凭据**只从环境变量读取**，并按下表配置（`HYDRO_LOGIN_URL` 决定“配置源”）：

| 环境变量 | 必填 | 说明 |
| --- | --- | --- |
| `HYDRO_LOGIN_URL` | 是 | 登录页 URL；其 `协议 + 主机 + 端口` 即受信任配置源 |
| `HYDRO_USERNAME` | 是 | 表单账号字段的值 |
| `HYDRO_PASSWORD` | 是 | 表单密码字段的值 |
| `HYDRO_AUTH_URL` | 否 | 首页“采集配置站点日报”快捷入口的目标 URL（须同源） |
| `HYDRO_EXPIRE_URL` | 否 | 并发演示用的会话失效入口（须同源） |
| `HYDRO_CSRF_FIELD` | 否 | CSRF 字段名，默认 `csrf_token`；找不到时自动识别含 `csrf/token` 的字段 |
| `HYDRO_USER_FIELD` | 否 | 账号字段名，默认 `username` |
| `HYDRO_PASSWORD_FIELD` | 否 | 密码字段名，默认 `password` |
| `HYDRO_HTTP_TIMEOUT` | 否 | 单次 HTTP 超时秒数，默认 15 |
| `HYDRO_USER_AGENT` | 否 | 抓取 User-Agent |

### 会话与并发语义

- **只保留最近一次登录会话**：会话以不可变快照 + 世代号保存；新登录成功后
  整体替换旧会话。
- **401 自动重登**：抓取返回 401 时自动重登并重试；**每次抓取最多重试一次**，
  重试仍为 401 立即报错停止。
- **并发合并重登**：多个线程拿着同一过期会话同时收到 401 时，在同一把锁内
  只执行一次登录，其余线程直接使用新会话（single-flight）。
- **迟到的旧 401**：旧请求的 401 晚于其他线程重登成功才返回时，发现会话
  世代号已前进，直接**复用当前新会话**，不会再登录一次而把新会话作废。
- **严格源匹配**：Cookie 只发送给与 `HYDRO_LOGIN_URL` 的**协议、主机、端口**
  完全一致的地址；手动逐跳跟随重定向，跨源跳转不携带 Cookie；登录表单
  `action` 指向跨源地址时直接拒绝提交账号密码与 CSRF。
- **登录页不进入公示缓存**：登录流程的页面只用于提取 CSRF；若抓取结果仍是
  登录页（认证失败或会话异常）则渲染错误页，绝不把登录页写入公示缓存，
  认证失败会在网页上明确报错。

## 目录结构

```
.
├── app/
│   ├── web.py                 # Flask 路由：抓取/登录/选择/并发演示/转换/下载
│   ├── auth_fetch.py          # 登录流程、会话世代、401 合并重登、源匹配
│   ├── demo_site.py           # 本地日报演示站（登录、401、延迟 401、/expire）
│   ├── grid_parser.py         # 合并单元格展开、校验、表头路径、记录生成
│   ├── templates/             # index/select/preview/error 页面
│   └── static/
│       ├── style.css
│       └── samples/
│           ├── notice.html        # 标准示例公示页（含合法 rowspan/空值/短行）
│           └── notice_error.html  # 错误示例页（跨边界 rowspan、正文非法 colspan）
├── tests/                     # 45 个 unittest 用例（解析 + Web + 登录/并发）
├── requirements.txt
├── Dockerfile
├── docker-compose.yml         # hydro-collector + demo-site 两个服务
├── .env.example               # 演示环境变量模板
└── run_dev.sh                 # 本地无 Docker 时的开发启动脚本（同时起两站）
```

## 方式一：Docker Compose（推荐，交付即启）

```bash
docker compose up --build
# 采集 Web 入口（主应用）
http://localhost:8000
# 本地日报演示站（登录后可见日报）
http://localhost:8081/login     # 默认账号 hydro / hydro-demo-pass
```

Compose 会自动：

- 启动 `demo-site`（内部 `http://demo-site:8080`）与 `hydro-collector`；
- 通过环境变量把登录地址、账号密码注入采集端；
- 等待演示站健康检查通过后再启动采集端。

凭据可用 `.env`（参考 `.env.example`）或宿主机环境变量覆盖：

```bash
DEMO_USERNAME=myuser DEMO_PASSWORD=mypass docker compose up --build
```

### 演示路径

1. **原有离线示例**：打开 `http://localhost:8000`，点击「使用标准示例页」，
   选择表格、表头行数保持 **2 行**，预览 35 条观测记录并下载 CSV；
   「错误示例页」演示结构错误定位与停止转换。
2. **登录站点日报**：首页「登录站点（部署配置）」卡片点击「采集配置站点日报」，
   系统自动完成“GET 登录页取 CSRF → POST 凭据 → 带会话 Cookie 抓取日报”，
   随后可选择表格、转换并下载 CSV（4 条正文行 × 5 个观测列 = 20 条记录）。
3. **并发采集演示**：点击「运行并发采集演示」（也可直接访问
   `http://localhost:8000/demo/concurrent`）。系统先让服务端会话失效，再让
   4 个线程携带同一旧会话并发抓取，其中一个请求延迟 1.2 秒才返回旧 401。
   结果页会显示：4/4 成功、重登次数 **+1**（合并重登）、会话世代前进
   （如 `1 → 2`）、1 个延迟旧 401 线程同样成功，并直接完成正确转换。
4. **认证失败演示**：用错误的 `HYDRO_USERNAME/HYDRO_PASSWORD` 启动采集端后
   再抓取日报，网页显示“认证失败”错误页；登录页不会出现在表格选择/公示缓存中。
5. **演示站直接入口**：`http://localhost:8081/login` 登录后查看日报；
   `GET /expire` 可手动让当前会话失效，`GET /stats` 查看登录次数与会话状态。

## 方式二：本地 Python 运行

需要 Python 3.11+：

```bash
python3 -m pip install -r requirements.txt
./run_dev.sh            # 同时启动演示站 :8081 与采集 Web :8000
# 或分别开发热更：
FLASK_APP=app.demo_site flask run --host 0.0.0.0 --port 8081
FLASK_APP=app.web flask run --host 0.0.0.0 --port 8000
```

本地运行需要让采集端与实际登录地址同源，例如：

```bash
HYDRO_LOGIN_URL=http://127.0.0.1:8081/login \
HYDRO_AUTH_URL=http://127.0.0.1:8081/daily \
HYDRO_EXPIRE_URL=http://127.0.0.1:8081/expire \
HYDRO_USERNAME=hydro HYDRO_PASSWORD=hydro-demo-pass \
FLASK_APP=app.web flask run --port 8000
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```

共 45 个用例，覆盖：

- 表宽确定、表头去重与同路径多列保留、rowspan 继承、空值/短行、
  坐标 1 基计数、四类结构错误定位、错误即停；
- 登录流程（CSRF、Cookie）、会话过期 401 自动重登、最多一次重试；
- 并发同过期会话合并重登、延迟旧 401 复用新会话且不再登录；
- 站点只保留最近一次登录会话；
- 跨源重定向不携带 Cookie/凭据、跨源登录表单 action 拒绝提交；
- 登录页不进入公示缓存、认证失败网页报错；
- 认证站点全流程（抓取 → 选表 → 转换 → CSV）与并发演示页。

## 设计约束与说明

- 只解析目标 `<table>` 的直属 `<tr>/<td>/<th>`，嵌套表格不会串入。
- `rowspan=0`、非法跨度属性按浏览器约定按 1 处理；垂直方向超出表格
  底部的 rowspan 按浏览器约定裁剪，**横向**超出表宽判越界报错。
- 抓取缓存为进程内字典（TTL 30 分钟、上限 50 条），因此 Compose
  使用单 gunicorn worker + 多线程；登录会话与“合并重登”同样依赖单进程。
  如需多 worker/多实例，请替换为 Redis 等共享缓存与会话锁。
- 抓取远程页面时携带自定义 User-Agent、15 秒超时，并按响应头/内容
  推断编码，适配中文公示页常见的 GB 系编码。
- `demo_site` 仅用于本地/演示环境；其 `/expire` 等控制端点不设鉴权，请勿用于生产。
