# CloudDrive2 Telegram 下载管理器

**版本: 1.1.6**

项目简介：
这是一个专为 CloudDrive2 (CD2) 开发的 Telegram 机器人助手。它能够接收磁力链接、HTTP 链接及 ed2k 链接，并自动提交至 CD2 执行离线下载，同时提供强大的自动化后期清理功能。

---

## ✨ 功能特性

* 多协议支持：支持直接发送 magnet:?xt=、http://、https:// 以及 ed2k:// 链接进行离线下载。
* 智能后期清理：
    - **递归扫描**：深度扫描下载目录及所有子目录。
    - **小文件清理**：删除体积小于设定阈值（默认 300MB）的所有文件。
    - **黑名单过滤**：对大于阈值的文件，检查是否匹配黑名单关键词（如广告、.url、.txt 等），匹配则删除。
    - **空目录移除**：文件清理后，自动删除变为空的子目录。
* 网络代理支持：支持 http 和 socks5 代理，解决国内服务器无法连接 Telegram API 的问题。
* 轮询看门狗：周期检查 Telegram 轮询协程是否已停止，一旦发现异常静默就主动退出，交由 Docker 重启自愈，避免「容器在跑却收不到消息」。
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
      - NETWORK_ERROR_RESET_SECONDS=300      # 网络异常静默多久后重新计数（秒）
      - WATCHDOG_INTERVAL_SECONDS=60         # 轮询看门狗检查周期（秒），0 表示关闭
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
| SIZE_THRESHOLD | 否  | 300 | 文件体积小于此值(MB)将被删除，大于等于此值时检查黑名单 |
| PROXY_URL      | 否  | - | 连接 Telegram 的代理，支持 http/socks5 |
| NETWORK_ERROR_RESET_SECONDS | 否 | 300 | 网络异常静默达到此秒数后开始新一轮计数，仅用于日志诊断 |
| WATCHDOG_INTERVAL_SECONDS | 否 | 60 | 轮询看门狗的检查周期（秒），设为 0 可关闭看门狗 |
| CLEAN_CRON     | 否  |  30 3 * * * | 定时清理任务的 Cron 表达式|


---

## 🤖 指令说明

* 直接发送链接：发送磁力、HTTP 或 ed2k 链接，机器人自动提交下载任务。
* /clean：递归扫描目录，删除小文件和黑名单文件，清理空目录。
* /blacklist：查看当前已设置的黑名单关键词。
* /blacklist [关键词]：动态添加新的过滤关键词。

---

## 🛠️ 更新日志

### v1.1.6 (2026-10-08)
* **修复「永久静默」故障**：当 Telegram 返回 401/404 时，`python-telegram-bot` 会将其映射为 `InvalidToken` 并直接终止轮询协程，且不经过全局错误处理器。此前表现为「容器 running、重启次数 0、却再也收不到任何消息」。本版新增**轮询看门狗**，周期性检查轮询协程是否仍然存活，一旦发现已停止就记录 CRITICAL 并主动退出进程，交由 Docker 的 `restart` 策略重启自愈。
* **新增看门狗配置**：`WATCHDOG_INTERVAL_SECONDS` 默认 60 秒，设为 `0` 可关闭看门狗。
* **说明**：v1.1.5 的代码此前已合并进主干，但从未发布过镜像 tag；v1.1.6 是首个包含网络异常修复的正式发布镜像。

### v1.1.5 (2026-07-15)
* **修复网络异常累计问题**：网络恢复后不再把历史错误永久累计到退出阈值
* **避免容器重启循环**：Telegram 或代理网络异常不再主动停止应用，交给内置轮询机制自动重连
* **新增异常窗口配置**：`NETWORK_ERROR_RESET_SECONDS` 默认 300 秒，仅用于分组记录诊断日志

### v1.1.4 (2026-06-08)
* **重构清理逻辑**：
    - 改为递归扫描所有子目录
    - 文件级删除判断：体积 < 阈值直接删除，体积 >= 阈值时检查黑名单
    - 清理文件后自动删除空目录
* **修复误删问题**：解决旧逻辑可能误删包含大文件的文件夹的问题

### v1.1.3 (2026-05-06)
* **新增网络重试次数限制**：添加 `MAX_RETRIES` 环境变量（默认10次），避免无限重试浪费资源
* **智能计数器重置**：网络恢复时自动重置重试计数器
* **修复启动崩溃**：移除不存在的 `run_polling` 参数（`retry_on_error` 等）
* **优化 CI 构建**：只在发布 tag 时构建镜像，同时生成版本号和 `latest` 标签

### v1.1.2 (2026-05-06)
* **修复代理配置**：为 Updater 补充 `get_updates_request` 代理配置，解决已读不回问题
* **增强错误日志**：对网络错误添加更详细的提示信息

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
