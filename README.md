# Buddy 签到管理台

WorkBuddy（Buddy 加油站）多账号自动签到 / 连续登录 / 连登奖励领取 / 派猫猫旅行，带 Web 管理台与 Docker 部署。

## 功能

| 功能 | 说明 |
|---|---|
| 每日签到 | 自动领取 Buddy 加油站积分（每天 100） |
| 连续登录 | 每日发一条真实对话，满足活跃地图打卡条件 |
| 连登奖励 | 自动检查并领取 7 / 14 / 28 天档奖励 |
| 派猫猫旅行 | 每日自动派出，Buddy 归来后自动领取积分 |
| 多账号 | 每个账号独立 token、状态、日志，互不影响 |
| Web 管理台 | 账号增删改、token 导入、立即执行、日志查看、通知设置 |
| 微信推送 | 管理台里填机器人地址就推、清空就不推（默认不推） |
| 定时调度 | 每天多个时间点自动执行，可配多个兜底时间 |

## 一键部署

```bash
git clone https://github.com/zljiscx/buddysignin.git
cd buddysignin
cp .env.example .env
```

编辑 `.env`（保持默认也能跑）：

```ini
BUDDY_IMAGE=zljiscx/buddysignin:latest
BUDDY_PORT=18321
BUDDY_ADMIN_PASSWORD=换成你自己的密码
```

启动：

```bash
docker compose up -d
```

打开 `http://<主机IP>:18321`，输入密码进入管理台。

> 镜像从 Docker Hub 拉取（amd64 + arm64）。国内直连 Docker Hub 慢或超时的话，把上面那行换成
> `ghcr.io/zljiscx/buddysignin:latest`（同一份镜像，免登录）。
> 默认端口特意选了不常用的 **18321**，避免和群晖/其他服务抢 8080。

## 镜像与自动构建

`.github/workflows/docker.yml` 在推送到 `main` 或打 `v*` 标签时自动构建 amd64 + arm64 镜像，同时推两个仓库：

| 仓库 | 地址 | 用途 |
|---|---|---|
| Docker Hub | `zljiscx/buddysignin` | 主用，`docker compose up -d` 默认拉这个 |
| GHCR | `ghcr.io/zljiscx/buddysignin` | 备用，Docker Hub 拉不动时换这个 |

两个仓库都已在 CI 里配好，push 即自动更新，平时不用管。
国内直连 Docker Hub 有时很慢或超时，那就把 `.env` 的 `BUDDY_IMAGE` 换成 `ghcr.io/zljiscx/buddysignin:latest`（实测免登录可拉）。

> fork 到自己账号后要启用 Docker Hub：仓库 Settings → Secrets and variables → Actions 加 `DOCKERHUB_USERNAME`（用户名）和 `DOCKERHUB_TOKEN`（Docker Hub → Account settings → Personal access tokens，勾 Read + Write）。没配时 CI 只推 GHCR，不会失败。

手动发版：

```bash
git tag v1.0.0 && git push origin v1.0.0
```

## 通知推送（企业微信）

不用改任何配置文件，也不用加环境变量：

1. 企业微信群 → 右上角 → 添加群机器人 → 复制 Webhook 地址
2. 管理台右上角 **「通知设置」** → 粘贴地址（或只填末尾的 `key`）→ 保存
3. 点「保存并测试」，群里收到测试消息即配置成功
4. 清空后保存 = 关闭推送

填了才推送，没填就完全不推。设置存在 `data/settings.json`，所有账号共用。

## 添加账号

1. 在已登录 WorkBuddy 的 Windows 电脑上运行：

   ```bash
   python buddy_auth.py
   ```

   它会自动定位客户端、解密登录态，并打印一行 **transfer code**，同时把账号登记到 `data/accounts/<uid>/`。

2. 在 Web 管理台点「+ 添加账号」，粘贴 transfer code（或明文 accessToken），保存即可。

3. 点「立即执行」验证；之后调度会自动运行。

> Token 有效期约 30 天，到期后在桌面端重新登录并重跑 `buddy_auth.py`，再到管理台重新粘贴即可。

## 目录结构

```
app.py                 # 主程序（Web + 调度）
buddy_checkin.py       # 单账号执行逻辑（签到/对话/领奖/旅行/推送）
buddy_auth.py          # 桌面端 token 解密导出
core/
  accounts.py          # 多账号存储
  runner.py            # 多账号执行与调度
  settings.py          # 全局设置（推送地址）
  webui.py             # Web 管理台后端
web/index.html         # 管理台页面
data/                  # 运行时数据（不入库）
  accounts.json
  accounts/<uid>/{token_store,checkin_state,cloud_state}.json, Log.txt
  buddy_config.json
  settings.json
```

## 本地运行（Windows 测试）

```bash
python app.py                  # Web(18321) + 调度，常驻
python app.py --once           # 立即执行一轮后退出（配合计划任务）
python app.py --web-only       # 只开 Web
python app.py --no-web         # 只跑调度
python app.py --port 19000     # 换端口
```

无第三方依赖，仅 Python 3.8+ 标准库。本地运行同样会读取根目录 `.env`
（管理台密码、端口），与容器行为一致；想临时不校验密码，把 `.env` 里的
`BUDDY_ADMIN_PASSWORD` 清空即可。

## 配置

`data/buddy_config.json`（账号目录下的同名文件可覆盖）：

```json
{
  "times": ["08:10", "12:40", "20:30"],
  "chat": { "enabled": true, "model": "hy3", "prompt": "只回复一个好字" },
  "growth": { "enabled": true },
  "travel": { "enabled": true, "location_id": 0 }
}
```

- `times`：每日执行时间点，多个互为兜底。旅行 1~4 小时返回，建议保留一次间隔 ≥4 小时的执行。
- `travel.location_id`：0 表示按日期轮换地点（咖啡馆 / 商场 / 健身房 / 古镇客栈）。
- 推送地址不在这个文件里，见上面的「通知推送」。

环境变量：`BUDDY_ADMIN_PASSWORD`、`BUDDY_PORT`、`BUDDY_HOST`。

## 注意

- `data/` 含明文 token，已在 `.gitignore` 中排除，请勿提交。
- 公网部署务必设置 `BUDDY_ADMIN_PASSWORD`。
- 本项目仅用于个人账号自动化，请遵守 WorkBuddy 的服务条款。
