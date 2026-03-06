# CloudDrive2 Telegram Manager



这是一个专为 **CloudDrive2 (CD2)** 离线下载开发的 Telegram 机器人助手。它能帮你通过手机随时随地提交下载任务，并根据规则自动清理下载目录中的垃圾文件和无效文件夹。

## 🌟 核心功能

- **远程离线下载**：发送磁力链接 (`magnet:?`) 或 HTTP 链接，直接推送到 CD2 离线任务。
- **自动菜单注册**：启动即自动同步 Telegram `/` 指令菜单，无需手动配置。
- **智能黑名单清理**：
  - 发送 `/clean` 即可扫描并删除指定的垃圾文件（如 `.url`, `.txt`, 各种广告文件）。
  - **自动删减**：如果任务文件夹内不含大于 **300MB** 的文件（判定为样片或无效任务），机器人会将其连根删除。
  - **空目录清理**：自动删除离线任务产生的空文件夹。
- **白名单安全机制**：仅允许设定的 `ADMIN_IDS` 用户使用。

## 🚀 部署指南 (Docker)

### 1. 准备文件
确保你的 GitHub 仓库中包含以下核心文件：
- `main.py`, `clouddrive_pb2.py`, `clouddrive_pb2_grpc.py`
- `Dockerfile`, `requirements.txt`

### 2. 运行环境配置 (docker-compose.yml)
推荐使用 `docker-compose` 部署，示例配置如下：

```yaml
services:
  cd2-bot:
    image: ghcr.io/你的用户名/你的仓库名:latest
    container_name: tg_cd2_manager
    restart: always
    volumes:
      - ./blacklist.txt:/app/blacklist.txt
    environment:
      - CD2_ADDRESS=192.168.x.x:19798       # CD2 的访问地址
      - CD2_TOKEN=你的_CD2_API_TOKEN        # CD2 设置中获取的 Token
      - TG_TOKEN=你的_TG_BOT_TOKEN          # 从 @BotFather 获取
      - SAVE_PATH=/115/离线下载             # 任务保存的根目录
      - ADMIN_IDS=1234567,8901234           # 允许使用机器人的 TG ID
      - SIZE_THRESHOLD=300                  # 判定垃圾任务的体积阈值(MB)