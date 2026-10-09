# CloudDrive2 Telegram 下载管理器

**版本: 1.1.10-5 (dev 预发布)**

项目简介：
这是一个专为 CloudDrive2 (CD2) 开发的 Telegram 机器人助手。它能够接收磁力链接、HTTP 链接及 ed2k 链接，并自动提交至 CD2 执行离线下载，同时提供强大的自动化后期清理功能。

---

## 🌿 分支与版本约定

开发迭代在 `dev` 分支进行，生产发布在 `master` 分支，两者用**镜像标签完全隔离**。

| 分支 | 版本号（`main.py` 的 `__version__`） | 推送后自动构建的镜像标签 | GitHub Release |
| --- | --- | --- | --- |
| `dev` | `1.1.10-1`、`1.1.10-2` ……（比生产版本 +1 并带 `-n`） | `dev-latest`、`dev-1.1.10-1` | 不创建 |
| `master`（推 `v*` tag） | `1.1.10`（去掉 `-n`） | `1.1.10`、`latest` | 自动创建 |

**为什么这么设计**：dev 上一天改十次也只产生 `dev-*` 镜像，生产环境不会被高频版本号追着升级；等一批改动稳定后，一次性同步到 `master` 并发一个正式版本。

### 开发迭代流程（dev）

```bash
git switch dev
git pull
# ...改代码...
# 1) 把 main.py 的 __version__ 递增：1.1.10-1 → 1.1.10-2
# 2) 同步 README 顶部版本行
git commit -am "fix: xxx"
git push origin dev
```

推送后 CI 自动构建 `ghcr.io/ymting/cd2_magnet_tgbot:dev-latest` 与 `:dev-1.1.10-2`，**不会碰 `latest`，也不会创建 Release**。

想测试这一版，把测试机的 compose 镜像改成 `:dev-latest`（或钉死 `:dev-1.1.10-2`）即可。

### 生产发版流程（master）

```bash
git switch master
git pull
git merge --no-ff dev            # 从 dev 同步代码（可能需解决冲突）
# 1) 把 main.py 的 __version__ 改为正式版：1.1.10-2 → 1.1.10
# 2) 同步 README 版本行 + 更新日志
git commit -am "release: v1.1.10"
git push origin master
git tag v1.1.10 && git push origin v1.1.10   # 触发正式镜像 + Release
```

发版后回到 `dev`，把版本号推进到下一个预发布位（`1.1.10` → `1.1.11-1`），避免两边版本号撞车。

---

## ✨ 功能特性

* 多协议支持：支持直接发送 magnet:?xt=、http://、https:// 以及 ed2k:// 链接进行离线下载。
* **批量提交**：一条消息里可以混着粘贴多个不同类型的链接（磁力、ed2k、HTTP 混排），机器人会**逐个提交**，
  然后汇总成一份报告，逐条标出成功/失败与原因。带序号（`1.`）、项目符号或前后带说明文字都能识别。
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
    container_name: cd2_magnet_tgbot
    restart: always
    volumes:
      # 可选：把黑名单挂到宿主机，容器重建后仍保留你添加的关键词；
      # 不挂载则黑名单只存在容器里，重建后恢复为默认列表
      - ./blacklist.txt:/app/blacklist.txt

    environment:
      # ==================== 必填项（缺一不可） ====================
      - CD2_ADDRESS=192.168.31.224:19798     # CloudDrive2 的 gRPC 地址，格式 IP:端口
      - CD2_TOKEN=你的_CD2_API_TOKEN         # CloudDrive2 设置里获取的 API Token
      - TG_TOKEN=你的_机器人_TOKEN           # 从 @BotFather 获取的机器人 Token
      - ADMIN_IDS=1234567,8901234            # 允许操作的用户数字 ID，多个用英文逗号分隔

      # ==================== 可选项（放最后；不配置就用下面注释里的默认值） ====================
      - SAVE_PATH=/115/离线下载              # 可选，默认 /115/离线下载；离线下载存放的根目录
      - SIZE_THRESHOLD=300                   # 可选，默认 300；小于该体积(MB)的文件会被清理删除
      - CLEAN_CRON=30 3 * * *                # 可选，默认 30 3 * * *（每天 03:30）；定时清理的 Cron 表达式
      - WATCHDOG_INTERVAL_SECONDS=60         # 可选，默认 60；轮询看门狗检查周期(秒)，设为 0 关闭看门狗
      - NETWORK_ERROR_RESET_SECONDS=300      # 可选，默认 300；网络异常静默多久后重新计数(秒)，仅用于日志诊断
      - PROXY_URL=                           # 可选，默认留空=直连；访问 Telegram 的代理，支持 http/socks5
```

> 上面「可选项」每行都写了不配置时的默认值，**用不到的直接删掉那一行即可**，效果与保留默认值完全相同。
> 想测试开发版镜像，把 `image` 换成 `ghcr.io/ymting/cd2_magnet_tgbot:dev-<版本号>`（例如 `dev-1.1.10-3`），详见上方「分支与版本约定」。

### 升级 / 回滚

```bash
docker compose pull && docker compose up -d     # image 方式：拉新镜像并重建容器
```

用 `image:` 时服务器上**不需要源码和 Dockerfile**，也不占本地构建资源。如果改用 `build: .` 在服务器本地构建，
命令必须带上 `--build`：

```bash
git pull && docker compose up -d --build
```

**少了 `--build` 会继续使用旧镜像** —— 表现为「代码明明更新了，机器人行为却没变」，这个坑很难自己发现。

### 常用运维命令（容器名 `cd2_magnet_tgbot`）

```bash
docker logs -f cd2_magnet_tgbot                  # 看实时日志
docker inspect -f '{{.State.Status}} exit={{.State.ExitCode}} restarts={{.RestartCount}}' cd2_magnet_tgbot
docker compose down && docker compose up -d       # 彻底重建
```

> 如果你之前用的是别的容器名（`cd2_tg_bot` / `tg_cd2_manager`），换名后第一次 `up -d` 可能报
> `container name ... is already in use` —— 先 `docker rm -f 旧名字` 再启动即可。

---

## 📖 环境变量详细说明

### 必填项（缺一不可）

| 变量名 | 默认值 | 描述 |
|:---|:---|:---|
| CD2_ADDRESS | 127.0.0.1:19798 | CloudDrive2 的 IP 和 gRPC 端口 |
| CD2_TOKEN | 无 | CloudDrive2 API 的 Access Token，留空则所有 CD2 调用都会失败 |
| TG_TOKEN | 无 | Telegram Bot 的 API Token，留空则机器人无法启动 |
| ADMIN_IDS | 无 | 允许操作的用户数字 ID，逗号分隔；留空则任何人都用不了 |

### 可选项（不配置即使用默认值，用不到可以直接删掉对应的行）

| 变量名 | 不配置时的默认值 | 描述 |
|:---|:---|:---|
| SAVE_PATH | `/115/离线下载` | 离线下载任务存放的根路径 |
| SIZE_THRESHOLD | `300` | 文件体积小于此值(MB)将被删除，大于等于此值时检查黑名单 |
| CLEAN_CRON | `30 3 * * *` | 定时清理任务的 Cron 表达式（默认每天 03:30） |
| WATCHDOG_INTERVAL_SECONDS | `60` | 轮询看门狗的检查周期（秒），设为 `0` 可关闭看门狗 |
| NETWORK_ERROR_RESET_SECONDS | `300` | 网络异常静默达到此秒数后开始新一轮计数，仅用于日志诊断 |
| PROXY_URL | 空（直连，不走代理） | 连接 Telegram 的代理，支持 http/socks5 |


---

## 🤖 指令说明

* 直接发送链接：发送磁力、HTTP 或 ed2k 链接，机器人自动提交下载任务。
    - **支持一次发多个**：一条消息里混着几个链接都行，机器人逐个提交后回一份汇总报告。
    - **一行一个链接直接贴**即可（换行、空格、制表符分隔都认）；带序号（`1.`）、项目符号（`-` `•`）、
      或者链接后面跟一句说明文字（「这个先下」）也都能正确识别。
    - 复制来的链接会被自动清洗：末尾粘的句号/书名号等标点、被反引号或方括号包住的链接、
      Markdown 链接语法（`[标题](链接)`）、以及夹带的零宽字符，都会处理干净再提交；
      同一条消息里重复粘贴的链接只提交一次。
    - ⚠️ 单条消息上限 4096 字符。**贴特别长的列表时 Telegram 会自己拆成多条消息发送**，
      机器人会对每条消息各回一份报告（这是正常现象，不是重复提交）。
* /clean：递归扫描目录，删除小文件和黑名单文件，清理空目录。
* /blacklist：查看当前已设置的黑名单关键词。
* /blacklist [关键词]：动态添加新的过滤关键词。

---

## 🛠️ 更新日志

### v1.1.10-5 (dev 预发布，未发生产)
* **支持一次粘贴多个链接**：现在可以把攒好的链接一起发给机器人（磁力、ed2k、HTTP 混在一起也行），
  它会**逐个提交**，再回一份汇总报告，逐条标出哪个成功、哪个失败、失败是什么原因。
  ```
  📊 批量提交完成：成功 2 / 失败 2（共 4 个链接）
  📂 目录：/115/离线下载

  1. ✅ magnet:?xt=urn:btih:AAAA…(共 84 字符)
  2. ❌ ed2k://|file|某部电影.avi|1073741824|…(共 67 字符)
        ↳ ⚠️ 这个链接之前已经提交过了，无需重复提交。
  3. ❌ https://example.com/video.mp4
        ↳ ❌ 提交失败，CD2 连接异常（AioRpcError）：failed to connect to all addresses
  4. ✅ https://example.com/another.mp4
  ```
* **带序号/带说明文字也认**：`1. magnet:... 2. ed2k://...`，或者「这两个帮我下 magnet:... 和 ed2k://...」都能识别。
  旧实现要求整条消息以链接开头，遇到序号会**整条丢弃**，一个都提交不了。
* **换行粘贴是主场景，已覆盖**：一行一个链接直接贴（LF / CRLF / 空行 / 制表符 / 全角空格都认）。
  另外从网页、App、Markdown 里复制链接常见的几种「包裹」也一并清洗：
  `` `链接` ``、`[链接]`、`(链接)`、`<链接>`、`[标题](链接)`，以及夹带的零宽字符。
  清洗原则是「链接主体里已有对应的开启符号就保留」，所以 `https://zh.wikipedia.org/wiki/Foo_(bar)`
  和 `http://[::1]:8080/x` 这类 URL 自身的括号不会被误剥。
* ⚠️ 单条消息上限 4096 字符，**贴很长的列表时 Telegram 会拆成多条消息**，机器人各回一份报告。
* **每条链接单独提交**：因此成败能精确归到具体链接上；某一条失败（重复、云盘拒绝、连不上）不会影响其余链接继续提交。
* **顺手做的清洗**：复制来的链接末尾粘着句号/顿号/书名号会自动剥掉；
  同一条消息里重复粘贴的链接只提交一次，不会白挨一次「重复提交」的提示。
  （注意：`https://zh.wikipedia.org/wiki/Foo_(bar)` 这种以括号结尾的链接不会被误剥。）
* **报告用纯文本发送**：链接里的 `_` `*` `[` 会破坏 Telegram 的 Markdown 解析，纯文本没这个问题；
  链接特别多时会自动截断成「只展示前 N 条」，避免超长导致整条报告发不出去。

### v1.1.10-4 (dev 预发布，未发生产)
* **容器名定为 `cd2_magnet_tgbot`**（与仓库/镜像同名，便于 `docker logs`、`docker exec` 时一眼对上），根 compose 与 README 示例同步。
* **升级提醒**：如果你的服务器上跑着的容器是旧名字（`cd2_tg_bot` / `tg_cd2_manager`），直接 `docker compose up -d` 可能报
  `container name ... is already in use`。先删掉旧容器再起即可：`docker rm -f cd2_tg_bot`。

### v1.1.10-3 (dev 预发布，未发生产)
* **修掉 compose 里的 `build: .`**：仓库根 `docker-compose.yml` 原本写的是本地构建，而文档里写的升级方式是
  `docker compose pull` —— **两者对不上**，照文档做会拉到空、照文件做又必须记得加 `--build`。
  现统一改为 `image: ghcr.io/ymting/cd2_magnet_tgbot:latest`，与 README 示例一致；
  本地构建与 dev 测试镜像都以注释形式保留在文件里。
* **统一容器名**：README 示例里的 `tg_cd2_manager` 与根 compose 的 `cd2_tg_bot` 不一致，现两处保持一致。
* **文档补「升级 / 回滚」小节**：明确 `--build` 漏了会继续用旧镜像这个坑。

### v1.1.10-2 (dev 预发布，未发生产)
* **配置模板更清晰**：`docker-compose.yml` 与 README 的 compose 示例统一改为「必填项在前、可选项在后」，
  每个可选项都标注了**不配置时的默认值**（默认值逐条与 `main.py` 的 `os.getenv` 核对过），用不到的直接删行即可。
* **修掉示例里的代理占位值**：`PROXY_URL` 原先写死 `http://192.168.31.10:7890`（作者内网的示例地址），
  直接照抄会让机器人去连一个不存在的代理；现改为留空，并在注释里说明「留空 = 直连」。
* **文档补一句黑名单挂载是可选**：不挂载卷则黑名单只存在容器内，重建后恢复默认列表。
* 环境变量说明表格拆成「必填项 / 可选项」两张表，可选项表的列名直接叫「不配置时的默认值」。

### v1.1.10-1 (dev 预发布，未发生产)
* **修复 v1.1.9 漏掉的路径**：重复提交链接时仍会看到一大段技术报错，形如
  `❌ 提交失败，CD2 连接异常: AioRpcError: <AioRpcError of RPC that terminated with: status = StatusCode.INTERNAL
  details = "api error Cloud 115open(5975675)… code: 10008, message: 任务已存在，请勿输入重复的链接地址" …>`。
  原因是云盘的业务拒绝其实有**两条到达路径**：v1.1.9 只处理了 `success=false + errorMessage`，
  而 115open 对重复链接返回的是 **gRPC 异常**（`StatusCode.INTERNAL` + `code 10008`）。本版把异常路径一并归类，
  现在同样只显示「⚠️ 这个链接之前已经提交过了，无需重复提交。」
* **不再把业务拒绝误报成「连接异常」**：只有 `UNAVAILABLE` / `DEADLINE_EXCEEDED` 这类真正的传输层故障才说「CD2 连接异常」，
  `INTERNAL` 属于云盘 API 业务错误，如实说「提交失败」。
* **用户消息里不再出现多行 repr**：`AioRpcError` 的 `details` 与 `debug_error_string` 本就是同一份内容，
  现在只取 `details()` 并压成单行 + 截断，消息里不会再出现 4 行堆栈样式的内容。
* **顺带修掉同类问题**：`/clean` 的失败回执与单目录清理异常原本也会把 `AioRpcError` 整段原文塞进 Telegram，
  现已统一压成一行摘要（清理失败回执同时去掉 Markdown 解析，避免异常文本里的特殊字符导致发送再失败）。
* **新增 dev 分支构建流水线**：推 `dev` 分支会自动构建 `dev-latest` 与 `dev-<版本号>` 镜像，**不触碰 `latest`、不创建 Release**；
  版本号由 CI 直接从 `main.py` 的 `__version__` 读取，避免 tag 名与代码版本号两处手写不一致。详见上方「分支与版本约定」。

### v1.1.9 (2026-10-09)
* **提示文案口语化**：重复提交同一个链接时，之前机器人会把 CloudDrive2 返回的原始错误（面向开发者的技术描述，可能夹带云盘 API 原文和错误码）原样甩出来，比如「❌ CD2 拒绝请求: 添加离线下载任务失败: …」。现在改为归类成一句人话：**「⚠️ 这个链接之前已经提交过了，无需重复提交。」**
* **顺带归类其它常见拒绝原因**：CD2 授权失效 → 提示检查 `CD2_TOKEN`；链接不被支持 → 直接说明 CD2 无法离线下载该链接；其余未知错误去掉无信息量的动作前缀并截断展示。
* **原文仍完整留在日志**：简化只针对 Telegram 回复，`docker logs` 里依然能看到 CD2 返回的原始 `errorMessage`，排查不受影响。

### v1.1.8 (2026-10-09)
* **修复「提交失败」误报**：发送磁力链接时偶尔提示「❌ 提交失败，CD2 连接异常: httpx.ConnectError」，但云盘里任务其实已经提交成功。原因是同一个 `except` 同时包住了「提交到 CD2」和「给用户回执」两件事，回执走 Telegram（经代理）时的网络抖动被错报成了 CD2 故障。现在两者分开归因：**只有 CD2 侧失败才会提示「CD2 连接异常」**，回执失败只记日志，并明确标注「任务已提交成功」。
* **回执发送增加重试**：Telegram/代理侧的瞬时断连（`httpx.ConnectError`）重建连接通常即可恢复，现在最多重试 3 次；重试仍失败会留下带堆栈的 ERROR 日志，不再出现「用户看到报错、日志里却查不到」的情况。
* **补齐异常信息**：CD2 故障提示会带上异常类型名，便于区分 gRPC 不可用与业务拒绝。
* **顺带修复同类问题**：`/clean` 的清理报告发送原本也在 `try` 内，Telegram 发送失败会被伪装成「无法执行清理」；现已移出并单独归因，报告发送失败时自动降级为纯文本重发。
* **日志瘦身**：超长 magnet 链接写入日志时截断，避免 `dn` 参数刷爆日志。

### v1.1.7 (2026-10-08)
* **修复日志泄露凭据**：`httpx` 默认以 INFO 级别打印完整请求 URL，而 Bot Token 就嵌在 URL 路径里（`https://api.telegram.org/bot<ID>:<KEY>/getUpdates`），导致 `docker logs` 中出现明文 Token。现新增日志脱敏过滤器，把密钥替换为 `bot<TOKEN已脱敏>`，接口名与状态码保留，不影响排查网络问题。

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
