# CloudDrive2 Telegram 下载管理器



这是一个专为 **CloudDrive2 (CD2)** 开发的 Telegram 机器人助手。它能够接收磁力链接、HTTP 链接及 ed2k 链接，并自动提交至 CD2 执行离线下载，同时提供强大的自动化后期清理功能。

## ✨ 功能特性

- **多协议支持**：支持直接发送 `magnet:?xt=`、`http://`、`https://` 以及 `ed2k://` 链接进行离线下载。
- **智能后期清理**：
  - **黑名单过滤**：自动删除命中的广告、说明文件（如 `.url`, `.txt`, `扫码` 等）。
  - **垃圾任务判定**：若任务文件夹内没有文件超过设定阈值（默认 **300MB**），则判定为无效任务并自动整体删除。
  - **空目录移除**：自动识别并清理离线任务产生的空文件夹。
- **网络代理支持**：支持 http 和 socks5 代理，解决国内服务器无法连接 Telegram API 的问题。
- **自动命令菜单**：机器人启动后会自动向 Telegram 注册 `/clean` 和 `/blacklist` 命令菜单。
- **安全保障**：严格校验 `ADMIN_IDS`，仅限管理员操作；所有清理操作严格锁定在 `SAVE_PATH` 范围内。

## 🛠️ 部署指南 (Docker Compose)

推荐使用 Docker Compose 进行部署。在您的服务器上创建目录并编写 `docker-compose.yml`：

```yaml
services:
  cd2-bot:
    image: ghcr.io/你的用户名/你的仓库名:latest
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
      - PROXY_URL=[http://192.168.31.10:7890](http://192.168.31.10:7890)  # 可选：访问 Telegram 的代理地址