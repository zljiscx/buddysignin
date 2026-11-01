# Buddy 签到管理台

WorkBuddy（Buddy 加油站）多账号自动签到、连续登录、领取连登奖励、派猫猫旅行，带 Web 管理台，Docker 部署。

## 部署

```bash
git clone https://github.com/zljiscx/buddysignin.git
cd buddysignin
docker compose up -d
```

打开 `http://<主机IP>:18321`，默认密码 `admin`，登录后先到「设置」改成自己的。
端口冲突就改 `docker-compose.yml` 里 `18321:18321` 的左边数字；Docker Hub 拉不动就换成
`ghcr.io/zljiscx/buddysignin:latest`（同一份镜像，免登录可拉）。

## 使用

- **加账号**：在已登录 WorkBuddy 的 Windows 上跑 `python buddy_auth.py`，把打印的 transfer code 粘贴到管理台「+ 添加账号」。
- **时间点**：「设置」里配多个，互为兜底；当天每个点只触发一次，重启会补跑没做完的。
- **推送**：「设置」里填企业微信群机器人地址，留空则不推；只在本轮真做成事或出错时推，幂等跳过不推。
- **Token**：每次执行前自动续期，连续 30 天以上没跑才需要重新导入。

## 运行方式

```bash
python app.py          # 常驻：Web + 到点执行
python app.py --once   # 跑一轮就退出，交给 cron / 计划任务
```

两种方式状态互通，当天已完成的步骤都会跳过。

## 配置

| 路径 | 内容 |
|---|---|
| `data/settings.json` | 管理台设置（时间点、推送地址、密码） |
| `data/accounts/<uid>/` | 每个账号的 token、状态、日志 |
| `data/buddy_config.json` | 对话 / 领奖 / 旅行的开关与参数 |

`data/` 含明文 token，已在 `.gitignore` 中排除。

## 镜像

推送 `main` 或打 `v*` 标签时，GitHub Actions 构建 amd64 + arm64 镜像，同时推
`zljiscx/buddysignin` 与 `ghcr.io/zljiscx/buddysignin`。
fork 后想推 Docker Hub 需在仓库 Secrets 里加 `DOCKERHUB_USERNAME`、`DOCKERHUB_TOKEN`（勾 Read + Write），
不配则只推 GHCR。

> 仅用于个人账号自动化，请遵守 WorkBuddy 服务条款；公网暴露务必改掉默认密码。
