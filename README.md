# CloudDrive2 Telegram 下载管理器

**版本: 1.1.2**

项目简介：
这是一个专为 CloudDrive2 (CD2) 开发的 Telegram 机器人助手。它能够接收磁力链接、HTTP 链接及 ed2k 链接，并自动提交至 CD2 执行离线下载，同时提供强大的自动化后期清理功能。

---

## ✨ 功能特性

* 多协议支持：支持直接发送 magnet:?xt=、http://、https:// 以及 ed2k:// 链接进行离线下载。
* 智能后期清理：
    - 黑名单过滤：自动删除命中的广告、说明文件（如 .url, .txt, 扫码 等）。
    - 垃圾任务判定：若任务文件夹内没有文件超过设定阈值（默认 300MB），则判定为无效任务并自动整体删除。
    - 空目录移除：自动识别并清理离线任务产生的空文件夹。
* 网络代理支持：支持 http 和 socks5 代理，解决国内服务器无法连接 Telegram API 的问题。
* 自动命令菜单：机器人启动后会自动向 Telegram 注册 /clean 和 /blacklist 命令菜单。
* 安全保障：严格校验 ADMIN_IDS，仅限管理员操作。

---

## 🛠️ 部署指南 (Docker Compose)

推荐使用 Docker Compose 进行部署。在您的服务器上创建目录并编写 `docker-compose.yml`：

```yaml
services:
  cd2-bot:
    image: ghcr.io/ymting/cd2_magnet_tgbot:latest
    container_name: tg_cd2_manager
    restart: always
    volumes:
      - ./blacklist.txt:/app/blacklist.txt  # 持久化黑名单文件
    environment:
      - CD2_ADDRESS=192.168.31.224:19798    # CloudDrive2 的 gRPC 地址
      - CD2_TOKEN=你的_CD2_API_TOKEN         # CD2 设置中获取的 Token
      - TG_TOKEN=你的_机器人_TOKEN           # 从 @BotFather 获取的 Token
      - SAVE_PATH=/115/离线下载              # 下载保存的根目录
      - ADMIN_IDS=1234567,8901234            # 管理员数字 ID，多个用逗号隔开
      - SIZE_THRESHOLD=300                   # 判定垃圾任务的体积阈值 (MB)
      - PROXY_URL=http://192.168.31.10:7890  # 可选：访问 Telegram 的代理地址
      - CLEAN_CRON=30 3 * * *

```
---

## 📖 环境变量详细说明

| 变量名            | 必填 | 默认值 | 描述 |
|:---------------|:---| :--- | :--- |
| CD2_ADDRESS    | 是  | 127.0.0.1:19798 | CloudDrive2 的 IP 和 gRPC 端口 |
| CD2_TOKEN      | 是  | - | CloudDrive2 API 的 Access Token |
| TG_TOKEN       | 是  | - | Telegram Bot 的 API Token |
| ADMIN_IDS      | 是  | - | 允许使用机器人的用户数字 ID，逗号分隔 |
| SAVE_PATH      | 否  | /115/离线下载 | 离线下载任务存放的根路径 |
| SIZE_THRESHOLD | 否  | 300 | 文件夹内最大文件小于此体积(MB)将被删除 |
| PROXY_URL      | 否  | - | 连接 Telegram 的代理，支持 http/socks5 |
| CLEAN_CRON     | 否  |  30 3 * * * | 定时清理任务的 Cron 表达式|


---

## 🤖 指令说明

* 直接发送链接：发送磁力、HTTP 或 ed2k 链接，机器人自动提交下载任务。
* /clean：一键扫描并执行目录深度清理（删除黑名单文件及垃圾文件夹）。
* /blacklist：查看当前已设置的黑名单关键词。
* /blacklist [关键词]：动态添加新的过滤关键词。

---

## 🛠️ 更新日志

### v1.1.2 (2026-05-06)
* **新增轮询重试机制**：为 `run_polling` 配置指数退避重试参数，解决代理不稳定导致的连接中断问题
    - `reconnect_delay=2.0`：首次失败等待 2 秒
    - `error_backoff=1.5`：连续失败时采用指数退避策略
* **增强错误日志**：对 ConnectError/ConnectTimeout 等网络错误添加更详细的提示信息

### v1.1.1
* **修复代理配置**：为 Updater 补充 `get_updates_request` 代理配置，解决因 getUpdates 未走代理导致机器人无法收到指令（已读不回）的问题
* **适配 v22+ API**：解决配置 HTTPXRequest 时出现 'proxy_url' 意外参数的 TypeError

### v1.1.0
* **彻底解决假死问题**：改用 Telegram 原生 `JobQueue` 调度定时清理任务，避免 APScheduler 与 gRPC/Telegram 异步循环冲突

---

## 📝 开发者说明

项目基于 Python 开发，使用 gRPC 与 CloudDrive2 通信。
镜像构建通过 GitHub Actions 自动完成。

开源协议：MIT License