# 部署手册

覆盖四种部署形态：本机前台、macOS LaunchAgent（本项目默认）、Linux systemd、
Docker；以及对外暴露时的 Nginx/TLS 配置、安全清单、升级回滚与排障。

> 部署前务必确认：`MOSS_API_KEY` 有效且有 MOSS-Live 权限；服务器能访问
> `api.mosi.cn:443`（WSS）。用 `curl -I https://api.mosi.cn/v1/models` 快速验证网络。

---

## 0. 部署形态怎么选

| 场景 | 选择 | 命令 |
| --- | --- | --- |
| 自己电脑上试用，关机就停 | 前台运行 | `./scripts/run.sh` |
| 自己电脑长期可用、开机自启 | LaunchAgent | `./scripts/install_macos_launchd.sh install` |
| Linux 服务器常驻 | systemd | `deploy/moss-live.service` |
| 容器化/统一编排 | Docker Compose | `docker compose up -d --build` |
| 局域网或公网访问 | 以上任一 + Nginx TLS | `deploy/nginx.conf.example` |

**重要**：除 localhost 外，浏览器只在 **HTTPS** 下允许麦克风/摄像头。
用 `http://192.168.x.x:8000` 直接访问会拿不到麦克风，必须走 TLS 反向代理。

---

## 1. 基础环境

```bash
# 检查 Python（3.10+）
python3 --version

# 进入项目
cd /Users/sgk324/python/moss

# 虚拟环境 + 依赖
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 配置密钥
cp .env.example .env
$EDITOR .env      # 填 MOSS_API_KEY
```

目录权限：运行用户需要对项目目录有读写权限（写 `logs/`、`artifacts/`）。
端口：默认 `127.0.0.1:8000`，被占用时改 `APP_PORT` 或启动参数
`--port 8010`（同时改 Nginx 上游）。

自检：

```bash
python -c "from app.main import app; print('ok', len(app.routes))"
curl -s http://127.0.0.1:8000/api/health
```

---

## 2. 方式 A：本机前台运行

```bash
source .venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 8000
# 开发热重载：
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

浏览器打开 <http://127.0.0.1:8000>，右上角状态灯应为「服务正常」。
`Ctrl+C` 结束。

一键脚本（自动建 venv、装依赖、复制 `.env`）：

```bash
./scripts/run.sh              # 前台
PORT=8010 ./scripts/run.sh    # 换端口
RELOAD=1 ./scripts/run.sh     # 热重载
```

---

## 3. 方式 B：macOS LaunchAgent（本项目当前采用）

已在本机安装为 `com.moss.live`，登录后自动启动，异常退出 3 秒内自动重启。

```bash
# 安装（幂等，可重复执行）
./scripts/install_macos_launchd.sh install

# 状态 / 日志 / 重启 / 卸载
./scripts/install_macos_launchd.sh status
tail -f logs/moss-live.log
./scripts/install_macos_launchd.sh restart
./scripts/install_macos_launchd.sh uninstall
```

安装脚本会生成 `~/Library/LaunchAgents/com.moss.live.plist`，内容要点：

| 键 | 值 |
| --- | --- |
| `ProgramArguments` | `.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000` |
| `WorkingDirectory` | 项目根目录（保证能读到 `.env`） |
| `RunAtLoad` | `true` |
| `KeepAlive.SuccessfulExit` | `false`（非正常退出才重启） |
| `StandardOut/ErrorPath` | `logs/moss-live.log` / `logs/moss-live.err.log` |

改端口：`PORT=8010 ./scripts/install_macos_launchd.sh install`（会重写 plist 并重启）。

手工排查：

```bash
launchctl print gui/$(id -u)/com.moss.live | head -30
launchctl kickstart -k gui/$(id -u)/com.moss.live
```

---

## 4. 方式 C：Linux systemd

```bash
sudo useradd -r -m moss
sudo mkdir -p /opt/moss && sudo chown moss:moss /opt/moss
# 用 rsync/scp 把项目同步到 /opt/moss（不要带 .venv）
sudo -u moss python3 -m venv /opt/moss/.venv
sudo -u moss /opt/moss/.venv/bin/pip install -r /opt/moss/requirements.txt
sudo -u moss cp /opt/moss/.env.example /opt/moss/.env
sudo -u moss $EDITOR /opt/moss/.env       # 填密钥
sudo chmod 600 /opt/moss/.env

sudo cp /opt/moss/deploy/moss-live.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now moss-live
sudo systemctl status moss-live
sudo journalctl -u moss-live -f
```

---

## 5. 方式 D：Docker

```bash
cp .env.example .env && $EDITOR .env      # 填 MOSS_API_KEY
docker compose up -d --build
docker compose logs -f
docker compose ps
```

- 容器内监听 `0.0.0.0:8000`，宿主机只映射到 `127.0.0.1:8000`，避免裸奔。
- `env_file` 注入密钥，不打进镜像。
- 健康检查以 `/api/health` 为准。
- 停止/升级：`docker compose down && docker compose up -d --build`。

只要镜像：

```bash
docker build -t moss-live-demo:1.0.0 .
docker run -d --name moss-live -p 127.0.0.1:8000:8000 --env-file .env moss-live-demo:1.0.0
```

---

## 6. 方式 E：对外暴露（Nginx + TLS）

### 6.1 域名与证书

```bash
# DNS 把 moss.example.com 指向本机公网 IP
sudo apt install nginx certbot python3-certbot-nginx
sudo certbot --nginx -d moss.example.com     # 自动签发并续期
```

### 6.2 站点配置

复制 `deploy/nginx.conf.example` 到 `/etc/nginx/sites-available/moss-live`，
改 `server_name` 与证书路径，然后：

```bash
sudo ln -s /etc/nginx/sites-available/moss-live /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

关键点（配置里已包含）：

- `/ws/realtime` 必须 `proxy_http_version 1.1` +
  `Upgrade`/`Connection` 头，否则 WebSocket 升级失败（浏览器报 1006）。
- `proxy_read_timeout 3600s`：通话是长连接，默认 60s 会被掐断。
- `proxy_buffering off`：保证音频低延迟。

### 6.3 访问口令（可选）

`.env` 里设置 `APP_ACCESS_CODE=your-code`，然后访问
`https://moss.example.com/?code=your-code`。只适合临时分享，不是真正的认证体系；
正式对外建议在 Nginx 层加 Basic Auth 或 OAuth2 Proxy。

### 6.4 Caddy 备选（自动 HTTPS，更省事）

```caddyfile
moss.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Caddy 默认已处理 WebSocket 升级。

---

## 7. 安全清单

- [ ] `.env` 权限 600，且不在 git 里（`.gitignore` 已排除）。
- [ ] 服务只监听 `127.0.0.1`，对外统一走反向代理。
- [ ] 对外暴露时设置 `APP_ACCESS_CODE` 或加一层认证。
- [ ] 不在前端、日志、错误信息里输出完整 API Key（`/api/health` 只回末 4 位）。
- [ ] 定期轮换 Key；泄露后立即在平台吊销。
- [ ] 生产环境保持 `APP_TEST_ASSETS=false`、`APP_LOG_EVENTS=false`。
- [ ] 用 `MAX_SESSION_SECONDS` 限制单次通话时长，避免被长期占用。
- [ ] 在 Nginx 加 `limit_req` 限制建连频率（可选）。

---

## 8. 升级与回滚

```bash
# 升级
cd /Users/sgk324/python/moss
git pull                     # 或替换文件
source .venv/bin/activate
pip install -r requirements.txt
./scripts/install_macos_launchd.sh restart    # 或 systemctl restart moss-live

# 验证
curl -s http://127.0.0.1:8000/api/health | head -c 200
python scripts/e2e_live.py
```

回滚：保留一份上一个版本的目录（如 `moss-20261101/`），把 LaunchAgent 的
`WorkingDirectory` 指过去并 `restart` 即可；数据库/状态都在外部，回滚无数据迁移。

---

## 9. 监控与日志

**健康检查**：`GET /api/health`，建议外部探针 30 s 一次；`status != ok` 或
`api_key_configured == false` 时告警。

**关键指标**（`relay` 字段）：

| 指标 | 含义 | 异常信号 |
| --- | --- | --- |
| `active_sessions` | 当前通话数 | 长期不降 → 客户端未正常关闭 |
| `failed_connections` | 上游连接失败次数 | 持续增长 → Key/网络问题 |
| `rejected_messages` | 被本地拒绝的指令 | 增长 → 前端版本不匹配 |
| `bytes_up/down` | 累计流量 | 可估算带宽成本 |

**日志**（`logs/moss-live.log`）：

```
12:01:33 INFO    moss.relay | 会话开始 user=web-a1b2c3d4
12:02:10 INFO    moss.relay | 会话结束 reason=client_closed duration=37.2s up=428800B down=1966080B
```

`APP_LOG_EVENTS=true` 可打印每条协议事件（含上行指令与下行事件），排障时临时开启，
注意日志量。

**带宽估算**：上行 16 kHz × 16 bit = 32 KB/s，下行 48 kHz × 16 bit = 96 KB/s，
一次通话约 128 KB/s ≈ 0.46 GB/小时（不含视频帧，1 FPS JPEG ≈ 20~60 KB/s）。

---

## 10. 排障

| 现象 | 可能原因 | 处理 |
| --- | --- | --- |
| 页面 502 | 后端没起/端口不对 | `launchctl print`、`systemctl status`、`lsof -i:8000` |
| 状态灯「缺少密钥」 | `.env` 未填或未重启 | 填 `MOSS_API_KEY` 后 restart |
| 一直「正在连接」 | 上游 401 / 网络不通 | 看日志 `upstream_http_401` / `upstream_unreachable` |
| 连上就没声音 | 浏览器自动播放策略 | 用户点击后创建 AudioContext（本项目已如此），检查是否静音标签页 |
| 麦克风无输入 | 非安全上下文 | 用 localhost 或 HTTPS 访问 |
| 屏幕共享失败 | Safari 限制 / 用户取消 | 换 Chrome/Edge；确认在用户手势中调用 |
| 通话 30 分钟被断 | `MAX_SESSION_SECONDS` 默认 1800 | 调大配置或重连 |
| WebSocket 1006 | 反向代理未升级 / 超时 | 检查 `Upgrade` 头与 `proxy_read_timeout` |
| 改前端不生效 | 浏览器缓存 | 本项目静态资源 `no-store`，强制刷新一次 |

---

## 11. 本机部署现状（已执行）

| 项目 | 值 |
| --- | --- |
| 代码目录 | `/Users/sgk324/python/moss` |
| 虚拟环境 | `/Users/sgk324/python/moss/.venv`（Python 3.13.12） |
| 监听 | `http://127.0.0.1:8000` |
| 运行模式 | 生产模式：`APP_TEST_ASSETS=false`（`/test-assets` 返回 404） |
| 主进程 | `uvicorn app.main:app --host 127.0.0.1 --port 8000` |
| 日志 | `logs/moss-live.log`（自愈脚本与 LaunchAgent 共用） |
| 访问方式 | 浏览器打开 <http://127.0.0.1:8000> |
| 自启方式 | 已装两层：① 终端启动钩子（已生效）② LaunchAgent（plist 已就绪，需你手动注册一次） |

### 11.1 已生效的自启方式：终端启动钩子

部署环境里 `launchctl bootstrap` 被系统拒绝（`5: Input/output error`），
因此改用一条不依赖该权限的方案，**已经装好并实测通过**：

- 新增脚本 `scripts/autostart.sh`：幂等，已在运行则 3ms 内直接返回；
  没运行就 `nohup` 拉起 uvicorn，日志追加到 `logs/moss-live.log`。
- `~/.zshrc` 末尾追加了一个标记块，每次打开终端时后台调用它（`&!` 脱离作业控制，
  不拖慢终端、不打印任何东西）：

```zsh
# >>> MOSS Live autostart >>>
if [ -x /Users/sgk324/python/moss/scripts/autostart.sh ]; then
  /Users/sgk324/python/moss/scripts/autostart.sh --quiet >/dev/null 2>&1 &!
fi
# <<< MOSS Live autostart <<<
```

效果：电脑重启后，**只要打开任意一个终端窗口**，服务就会自动起来。
如果服务中途崩溃，再开一个终端窗口即可拉起。

日常操作：

```bash
cd /Users/sgk324/python/moss
./scripts/autostart.sh            # 确保在跑（会等就绪并打印 pid）
./scripts/autostart.sh --status   # 只看状态，退出码 0=运行中 1=未运行
./scripts/autostart.sh --stop     # 停止（下次开终端会再被拉起）
```

卸载钩子：删除 `~/.zshrc` 里上面那段标记块即可（改动前的备份在
`~/.zshrc.bak-moss-*`），然后 `./scripts/autostart.sh --stop`。

几个实现细节，都是踩过坑才补上的：

- 健康检查必须带 `curl --noproxy '*'`。本机环境有 `HTTP_PROXY` 指向本地代理，
  不绕过代理时健康检查会被转发出去，既慢又会误判成"服务没起"。
- 并发锁用 PID 文件而不是目录：目录锁在进程被强杀时会残留，导致之后所有启动
  被静默跳过。PID 文件锁会检查持有者是否还活着，死了就当陈旧锁接管。
- 变量后紧跟中文或全角符号时必须写 `${PORT}` 而不是 `$PORT`。某些 locale 下
  bash 会把全角字节并进变量名，报 `PORT?: unbound variable`。

### 11.2 更稳的一层：LaunchAgent（需你手动执行一次）

终端钩子依赖"你打开终端"这个动作。如果你希望**登录即启动、崩溃自动重启、
完全不需要开终端**，把 LaunchAgent 注册上（plist 已生成，`plutil -lint` 通过）：

```bash
cd /Users/sgk324/python/moss
./scripts/install_macos_launchd.sh install
./scripts/install_macos_launchd.sh status     # 应看到 state = running 与 pid
```

若仍报 `Input/output error`，通常是 plist 已注册但未加载，按顺序执行：

```bash
launchctl bootout gui/$(id -u)/com.moss.live 2>/dev/null
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.moss.live.plist
launchctl print gui/$(id -u)/com.moss.live | head -20
```

两层可以共存：LaunchAgent 已在跑时，终端钩子会直接返回，不会重复启动。
卸载 LaunchAgent：`./scripts/install_macos_launchd.sh uninstall`。

### 11.3 前台临时启动

```bash
cd /Users/sgk324/python/moss && ./scripts/run.sh
```

服务本身无状态，随时 kill 再起不影响使用（通话是即时会话，不做持久化）。

---

## 12. 换一台设备安装（迁移）

项目没有任何硬编码绝对路径，整个目录可以原样搬走。除 `.venv` 外全部文件加起来约 1 MB。

### 12.0 先决条件

| 项目 | 要求 |
| --- | --- |
| Python | 3.10 及以上（本项目在 3.13 验证；Debian/Ubuntu 还需 `apt install python3-venv`） |
| 网络 | 能访问 `api.mosi.cn:443`（WSS 上行下行都走它） |
| Node / ffmpeg | **不需要**。前端是零构建原生 ESM；ffmpeg 只在重新生成测试素材时才用 |
| 平台 | macOS / Linux 已验证。Windows 请用 WSL2 或 Docker，未做原生验证 |

### 12.1 在旧机器打包

```bash
cd /Users/sgk324
tar --exclude='moss/.venv' \
    --exclude='moss/logs' \
    --exclude='moss/artifacts' \
    --exclude='__pycache__' \
    -czf moss-live.tar.gz moss
ls -lh moss-live.tar.gz     # 约 1 MB
```

`.env` 要不要一起带走：

- **带走**（默认已在包里）：到新机器直接能跑。但里面是能花钱的 API Key，
  传输请走 AirDrop / `scp` / U 盘，别丢公共网盘或邮箱。
- **不带走**（`--exclude='moss/.env'`）：新机器上 `install.sh` 会从 `.env.example`
  生成一份空的，你手动填 key 即可。换设备/换人时更推荐这样。

### 12.2 在新机器安装

```bash
tar -xzf moss-live.tar.gz
cd moss
./scripts/install.sh              # 装依赖 + 生成 .env + 自检
./scripts/install.sh --start      # 装完直接前台启动
```

`install.sh` 按顺序做五件事，可反复执行（幂等，**从不覆盖已有 `.env`**）：

1. 检查 Python 版本（低于 3.10 直接报错退出，不会留下半个环境）
2. 创建 `.venv`（已存在则复用）
3. 安装 `requirements.txt`（用 `.deps-installed` 时间戳判断，未变更就跳过）
4. 生成 `.env`，并检查 `MOSS_API_KEY` 是否为空
5. 自检：导入 `app.main`、打印路由数、确认 `app/static` 前端文件完整

常见参数：`--port 9000`、`--python python3.12`、`--no-deps`、`--help`。

### 12.3 让它常驻

| 平台 | 做法 |
| --- | --- |
| macOS | `./scripts/install_macos_launchd.sh install`（登录自启、崩溃重启） |
| macOS（无 launchd 权限） | `./scripts/autostart.sh` + 把钩子加进 `~/.zshrc`（见 §11.1） |
| Linux | 用 `deploy/moss-live.service`，`systemctl enable --now moss-live` |
| 任意 | `docker compose up -d --build`（见 §5） |

> 注意：§11.1 的 shell 钩子里写的是本机的绝对路径
> `/Users/sgk324/python/moss/scripts/autostart.sh`。换设备或换目录时要同步改，
> 或者干脆改用 LaunchAgent / systemd。

### 12.4 从别的设备访问（手机、另一台电脑）

默认只监听 `127.0.0.1`，同网段的其他设备根本连不上。要开放访问，**三件事缺一不可**：

**第一步：改监听地址**

```bash
# .env
APP_HOST=0.0.0.0
```

**第二步：上 HTTPS（不做这步一定失败）**

浏览器只在"安全上下文"里给麦克风和摄像头：`localhost` 豁免，其余一律要求 HTTPS。
用 `http://192.168.1.20:8000` 打开页面能看到界面，但点通话会拿不到麦克风。
三种可行做法：

| 场景 | 方案 | 成本 |
| --- | --- | --- |
| 有域名 | Caddy 反代，自动签发证书（§6.4，最省事） | 域名 + 80/443 可达 |
| 只有局域网/无域名 | `mkcert` 生成局域网证书，装到手机信任列表 | 免费，需在每台设备装一次根证书 |
| 不想碰证书 | Tailscale / Cloudflare Tunnel，拿到一个 HTTPS 域名直连 | 免费额度够用，最省心 |

**第三步：设访问口令**

```bash
# .env
APP_ACCESS_CODE=换成一个只有你知道的字符串
```

`0.0.0.0` 暴露之后，中继会替**任何能连上它的人**用你的 Key 建会话。
不设口令等于把额度公开。口令配好后，页面会要求先输入才能通话。

接着按 §6 配 Nginx/Caddy 反代即可，WebSocket 的 `Upgrade` 头别漏（模板里已写好）。

### 12.5 更新已有安装

覆盖 `app/`、`scripts/`、`docs/` 后执行：

```bash
./scripts/install.sh          # requirements.txt 变了会自动重装依赖
./scripts/autostart.sh --stop
./scripts/autostart.sh        # 或 launchctl kickstart -k gui/$(id -u)/com.moss.live
```

`.env`、`logs/` 不在覆盖范围内，升级不会丢配置。
