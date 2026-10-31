# Buddy 签到管理台

WorkBuddy（Buddy 加油站）多账号自动签到 / 连续登录 / 连登奖励领取 / 派猫猫旅行，带 Web 管理台与 Docker 部署。

## 功能

| 功能 | 说明 |
|---|---|
| 每日签到 | 自动领取 Buddy 加油站积分（每天 100） |
| 连续登录 | 每日发一条真实对话，满足活跃地图打卡条件 |
| 连登奖励 | 自动检查并领取 7 / 14 / 28 天档奖励 |
| 派猫猫旅行 | 每日自动派出，Buddy 归来后自动领取积分 |
| Token 续期 | 每次执行前自动续期，无需手动换 token |
| 多账号 | 每个账号独立 token、状态、日志，互不影响 |
| Web 管理台 | 账号增删改、token 导入、立即执行、日志、设置、改密码 |
| 微信推送 | 管理台里填机器人地址就推、清空就不推（默认不推） |
| 定时调度 | 时间点在管理台里配，多个互为兜底，当天各触发一次 |

## 一键部署

```bash
git clone https://github.com/zljiscx/buddysignin.git
cd buddysignin
docker compose up -d
```

打开 `http://<主机IP>:18321`，输入**默认密码 `admin`** 进入，登录状态保持 7 天。
进去第一件事：右上角 **「设置」→ 管理密码** 改成自己的，保存后会自动退出，用新密码重登。

不需要 `.env`，也没有需要改的环境变量。端口被占用时改 `docker-compose.yml` 里
`- "18321:18321"` 的左边数字；想换镜像同理直接改 `image:` 那一行。

> 镜像从 Docker Hub 拉取（amd64 + arm64）。国内直连慢或超时的话，把 `image:` 换成
> `ghcr.io/zljiscx/buddysignin:latest`（同一份镜像，免登录可拉）。
> 端口特意选了不常用的 **18321**，避免和群晖等其他服务抢 8080。

## 自动执行时间点

管理台右上角 **「设置」→ 每日自动执行时间点**：左边是时间列表（每行显示启用 / 禁用），
右边是时间选择框和四个按钮。**先在左侧点选一行**，再用「修改 / 禁用 / 删除」操作它；
「增加」则用输入框里的时间新建一条。所有改动**立即保存并生效**，不用重启容器。

```
┌──────────────────────────┐  ┌──────────────┐
│ 08:10              启用  │  │  14:00   🕐  │
│ 10:00              启用  │  ├──────┬───────┤
│ 12:00              禁用  │  │ 增加 │ 修改 │
│ 14:00              启用  │  ├──────┼───────┤
└──────────────────────────┘  │ 禁用 │ 删除 │
                              └──────┴───────┘
```

规则：

- **多时间点兜底**：某天 08:10 因为网络或服务端波动没签到成功，12:40、20:30 会接着补。
- **禁用 = 留着但不用**：不删记录，只是不参与调度；想恢复点一下「启用」即可。
  全部禁用时不会自动执行（管理台会给出提示），重新启用后立刻恢复。
- **当天幂等**：签到、对话、连登奖励、猫猫旅行各自按「当日已成功」判断，成功过的步骤不会重复
  调接口；只补没做完的那部分。时间点本身也做记录（`data/scheduler_state.json`），
  同一时间点当天只触发一次。
- **重启补跑**：容器 14:00 才启动、当天还没跑过，会立刻补跑一轮，不会空等到明天。
- 旅行 1~4 小时才回来，建议时间点间隔 ≥4 小时，最后那个点才能顺手领到旅行积分。

## 两种运行方式

两种方式状态完全打通（都以 `data/accounts/<uid>/` 里的当日状态为准），可以任选一种，
也可以同时用——谁先成功就算完成，另一个只做补漏。

**① 常驻（容器默认就是这样）**

```bash
python app.py            # Web(18321) + 到点自动执行
python app.py --no-web   # 只跑调度，不开 Web
python app.py --web-only # 只开 Web，不自动执行
```

**② 定时启动（交给系统计划任务，不常驻）**

```bash
python app.py --once     # 跑一轮就退出，失败返回码 1
```

配合 Windows 任务计划程序 / cron / 群晖计划任务，想在几个时间点跑就配几条。
这种方式下 Web 里配的时间点不参与调度（没有常驻进程在读它），触发时间由计划任务决定；
但当天已完成的步骤依然会跳过，多触发几次也不会重复刷接口。

容器里也想用定时方式：

```bash
docker exec buddy-signin python app.py --once
```

## 登录与密码

- 默认密码 `admin`，只填密码、不需要用户名。
- 密码可随时在管理台「设置」里改；**改了之后所有已登录会话立即失效**，需要用新密码重登。
- 登录凭据是签名 Cookie（`data/session.key` 签名，HttpOnly，7 天）。
- 高级用法：容器启动时设环境变量 `BUDDY_ADMIN_PASSWORD` 可覆盖管理台密码，
  此时 Web 里改密码不生效（页面会提示）。
- 公网暴露时务必改掉默认密码。

## 通知推送（企业微信）

不用改配置文件，也不用加环境变量：

1. 企业微信群 → 右上角 → 添加群机器人 → 复制 Webhook 地址
2. 管理台右上角 **「设置」** → 粘贴到「群机器人地址」（或只填末尾的 `key`）→ 保存
3. 点「保存并测试推送」，群里收到测试消息即配置成功
4. 清空后保存 = 关闭推送

填了才推，没填就完全不推。设置存在 `data/settings.json`，所有账号共用。

## 添加账号

1. 在已登录 WorkBuddy 的 Windows 电脑上运行：

   ```bash
   python buddy_auth.py
   ```

   它会自动定位客户端安装目录、解密登录态，打印一行 **transfer code**，同时把账号登记到
   `data/accounts/<uid>/`。

2. 在 Web 管理台点「+ 添加账号」，粘贴 transfer code（或明文 accessToken），保存即可。

3. 点「立即执行」验证；之后按时间点自动运行。

Token 会在每次执行前自动续期（`/v2/plugin/auth/token/refresh`，网关代持密钥，无需任何配置），
卡片上「Token 剩余」下面会显示最近续期日期。只要每天有执行，就不会到期；
**连续 30 天以上没跑过**才需要重新跑 `buddy_auth.py` 导入。

## 目录结构

```
app.py                 # 主程序（Web + 调度 / --once 定时启动）
buddy_checkin.py       # 单账号执行逻辑（签到/对话/领奖/旅行/推送/续期）
buddy_auth.py          # 桌面端 token 解密导出
core/
  accounts.py          # 多账号存储
  runner.py            # 多账号执行与调度（时间点轮询、当日幂等）
  settings.py          # 全局设置（推送地址、时间点、密码）
  webui.py             # Web 管理台后端
web/
  index.html           # 管理台页面
  login.html           # 登录页
data/                  # 运行时数据（不入库）
  accounts.json
  accounts/<uid>/{token_store,checkin_state,cloud_state}.json, Log.txt
  buddy_config.json    # 其他配置项（对话开关、旅行地点等）
  settings.json        # 管理台设置
  scheduler_state.json # 当天已执行的时间点
  session.key          # 登录 Cookie 签名密钥
```

## 配置

管理台能改的（推送地址、时间点、密码）都在 `data/settings.json`，不用手改。
其余细项在 `data/buddy_config.json`（账号目录下的同名文件可覆盖）：

```json
{
  "chat": { "enabled": true, "model": "hy3", "prompt": "只回复一个好字" },
  "growth": { "enabled": true },
  "travel": { "enabled": true, "location_id": 0 }
}
```

- `travel.location_id`：0 表示按日期轮换地点（咖啡馆 / 商场 / 健身房 / 古镇客栈）。
- 时间点以管理台设置为准；只有管理台没配过时才读这里的 `times`。

## 镜像与自动构建

`.github/workflows/docker.yml` 在推送到 `main` 或打 `v*` 标签时自动构建 amd64 + arm64 镜像，
同时推两个仓库：

| 仓库 | 地址 | 用途 |
|---|---|---|
| Docker Hub | `zljiscx/buddysignin` | 主用，`docker compose up -d` 默认拉这个 |
| GHCR | `ghcr.io/zljiscx/buddysignin` | 备用，Docker Hub 拉不动时换这个 |

> fork 到自己账号后要启用 Docker Hub：仓库 Settings → Secrets and variables → Actions 加
> `DOCKERHUB_USERNAME`（用户名）和 `DOCKERHUB_TOKEN`（Docker Hub → Account settings →
> Personal access tokens，勾 Read + Write）。没配时 CI 只推 GHCR，不会失败。

手动发版：

```bash
git tag v1.1.0 && git push origin v1.1.0
```

## 注意

- `data/` 含明文 token，已在 `.gitignore` 中排除，请勿提交。
- 公网部署务必改掉默认密码。
- 本项目仅用于个人账号自动化，请遵守 WorkBuddy 的服务条款。
