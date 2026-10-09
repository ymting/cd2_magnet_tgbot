# -*- coding: utf-8 -*-
"""
项目名称: CloudDrive2 Telegram 离线下载管家
版本: 1.1.8
功能描述:
    1. 链接监听: 自动识别 Magnet、HTTP、ed2k 链接并提交至 CD2 离线下载。
    2. 定时清理: 基于 Cron 表达式，递归扫描下载目录，删除小文件和黑名单文件，清理空目录。
    3. 异常容错: 增加全局错误处理与 gRPC 超时控制，防止网络波动导致假死。
    4. 轮询看门狗: 周期检测 Telegram 轮询协程是否已死亡，避免「容器活着但收不到消息」的永久静默。
    5. 故障归因: 提交(CD2/gRPC)与回执(Telegram/httpx)分开处理，回执失败不再误报为「CD2 连接异常」。
作者: ymting
"""

import logging
import os
import re
import time
import grpc
import asyncio
import clouddrive_pb2
import clouddrive_pb2_grpc
from datetime import datetime

# 版本号
__version__ = "1.1.8"
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import Update, BotCommand
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters
from telegram.request import HTTPXRequest
from telegram.error import NetworkError, TimedOut

# ==========================================
# 1. 变量配置区 (从 Docker 环境变量读取)
# ==========================================
CD2_IP_PORT = os.getenv("CD2_ADDRESS", "127.0.0.1:19798")  # CD2 的内网 IP 和 gRPC 端口
CD2_TOKEN = os.getenv("CD2_TOKEN", "")  # CD2 API 授权令牌
SAVE_PATH = os.getenv("SAVE_PATH", "/115/离线下载")  # 下载存放的根路径
TG_BOT_TOKEN = os.getenv("TG_TOKEN", "")  # Telegram 机器人 Token
ADMIN_IDS = [int(i) for i in os.getenv("ADMIN_IDS", "").split(",") if i.strip()]  # 允许操作的用户 ID
PROXY_URL = os.getenv("PROXY_URL", "")  # 连接 Telegram 的网络代理
CLEAN_CRON = os.getenv("CLEAN_CRON", "30 3 * * *")  # 定时清理的 Cron 表达式
BLACKLIST_FILE = "blacklist.txt"  # 黑名单关键词存储文件
SIZE_THRESHOLD_MB = int(os.getenv("SIZE_THRESHOLD", "300"))  # 有效文件的最小体积阈值
NETWORK_ERROR_RESET_SECONDS = int(os.getenv("NETWORK_ERROR_RESET_SECONDS", "300"))
if NETWORK_ERROR_RESET_SECONDS <= 0:
    raise ValueError("NETWORK_ERROR_RESET_SECONDS 必须是大于 0 的整数")
# 轮询看门狗的检查周期(秒)。设为 0 表示关闭看门狗。
WATCHDOG_INTERVAL_SECONDS = int(os.getenv("WATCHDOG_INTERVAL_SECONDS", "60"))
if WATCHDOG_INTERVAL_SECONDS < 0:
    raise ValueError("WATCHDOG_INTERVAL_SECONDS 不能为负数")

# 配置日志输出，方便在 Docker 日志中查看运行状态
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class TokenRedactionFilter(logging.Filter):
    """把日志中出现的 Telegram Bot Token 替换为占位符，避免凭据随日志外泄。

    背景：httpx 会以 INFO 级别打印完整请求 URL，形如
        HTTP Request: POST https://api.telegram.org/bot<数字ID>:<密钥>/getUpdates "HTTP/1.1 200 OK"
    Bot Token 就嵌在 URL 路径里。这些日志落到 `docker logs`、被截图或被粘贴到
    聊天里求助时，等同于明文泄露密钥（拿到即可完全接管机器人）。

    这里只做脱敏、不做屏蔽：保留请求日志用于排查网络问题，但抹掉密钥本身。
    """

    # Telegram Bot Token 的形态固定：<bot 数字 ID>:<35 位左右 [A-Za-z0-9_-]>
    _TOKEN_PATTERN = re.compile(r"bot\d{6,}:[A-Za-z0-9_\-]{20,}")

    def filter(self, record: logging.LogRecord) -> bool:
        # httpx 走的是惰性格式化（msg 是模板、URL 在 record.args 里），
        # 必须先渲染成完整消息才能替换；替换后清空 args，避免 formatter 二次格式化。
        message = record.getMessage()
        if "api.telegram.org/bot" not in message:
            return True
        redacted = self._TOKEN_PATTERN.sub("bot<TOKEN已脱敏>", message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


# 挂在日志 handler 上，而不是某个具体 logger 上：
# Logger.filter 只在最初调用 handle() 的那个 logger 上执行，子 logger
# （httpx / httpcore / telegram）向上传播时不会经过 root logger 的 filter，
# 只有 handler 级别的 filter 才能拦住所有来源的记录。
for _handler in logging.getLogger().handlers:
    _handler.addFilter(TokenRedactionFilter())

# 网络异常只按时间窗口分组记录，不再作为停止应用的条件
_network_error_count = 0
_last_network_error_at: float | None = None


# ==========================================
# 2. 核心清理逻辑
# ==========================================

def get_blacklist():
    """读取黑名单配置，若文件不存在则创建默认列表"""
    if not os.path.exists(BLACKLIST_FILE):
        default_list = ["广告", "promo", ".url", "txt", "readme", "扫码", "最新地址"]
        with open(BLACKLIST_FILE, "w", encoding="utf-8") as f:
            for k in default_list: f.write(f"{k}\n")
        return default_list
    with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


async def get_all_items_recursive(stub, metadata, folder_path) -> tuple[list, list]:
    """
    递归获取文件夹下所有文件和目录
    返回: (文件列表, 目录列表)
    """
    files = []
    directories = []

    req = clouddrive_pb2.ListSubFileRequest(path=folder_path)
    sub_items = []
    async for reply in stub.GetSubFiles(req, metadata=metadata, timeout=15):
        if reply.subFiles:
            sub_items.extend(reply.subFiles)

    for item in sub_items:
        if item.isDirectory:
            directories.append(item)
            # 递归获取子目录中的内容
            sub_files, sub_dirs = await get_all_items_recursive(stub, metadata, item.fullPathName)
            files.extend(sub_files)
            directories.extend(sub_dirs)
        else:
            files.append(item)

    return files, directories


async def is_directory_empty(stub, metadata, dir_path) -> bool:
    """检查目录是否为空"""
    req = clouddrive_pb2.ListSubFileRequest(path=dir_path)
    sub_items = []
    async for reply in stub.GetSubFiles(req, metadata=metadata, timeout=15):
        if reply.subFiles:
            sub_items.extend(reply.subFiles)
    return len(sub_items) == 0


async def clean_task_folder(stub, metadata, folder_path) -> str | None:
    """
    对单个任务文件夹执行清理动作:
    - 递归扫描所有文件
    - 体积 < 阈值的文件删除
    - 体积 >= 阈值且匹配黑名单的文件删除
    - 清理空目录（不删除 folder_path 本身）
    """
    folder_name = os.path.basename(folder_path)
    try:
        # 递归获取所有文件和目录
        all_files, all_dirs = await get_all_items_recursive(stub, metadata, folder_path)
        logger.info(f"📁 扫描 `{folder_name}`: 发现 {len(all_files)} 个文件, {len(all_dirs)} 个目录")

        # 如果没有任何内容，直接删除空文件夹
        if not all_files and not all_dirs:
            await stub.DeleteFiles(clouddrive_pb2.MultiFileRequest(path=[folder_path]), metadata=metadata)
            return f"🗑️ 发现空目录已删除: `{folder_name}`"

        # 判断删除条件
        current_black = get_blacklist()
        threshold_bytes = SIZE_THRESHOLD_MB * 1024 * 1024
        files_to_delete = []

        for f in all_files:
            size_mb = f.size / (1024 * 1024)
            if f.size < threshold_bytes:
                # 体积 < 阈值，删除
                logger.debug(f"  🗑️ 标记删除(小文件): {f.name} ({size_mb:.1f}MB)")
                files_to_delete.append(f.fullPathName)
            elif any(k.lower() in f.name.lower() for k in current_black):
                # 体积 >= 阈值但匹配黑名单，删除
                logger.debug(f"  🗑️ 标记删除(黑名单): {f.name} ({size_mb:.1f}MB)")
                files_to_delete.append(f.fullPathName)
            else:
                logger.debug(f"  ✅ 保留: {f.name} ({size_mb:.1f}MB)")

        logger.info(f"  待删除文件数: {len(files_to_delete)}/{len(all_files)}")

        # 执行文件删除
        delete_count = 0
        if files_to_delete:
            await stub.DeleteFiles(clouddrive_pb2.MultiFileRequest(path=files_to_delete), metadata=metadata)
            delete_count = len(files_to_delete)

        # 清理空目录（从最深层开始）
        all_dirs.sort(key=lambda x: x.fullPathName.count('/'), reverse=True)

        for d in all_dirs:
            if await is_directory_empty(stub, metadata, d.fullPathName):
                await stub.DeleteFiles(clouddrive_pb2.MultiFileRequest(path=[d.fullPathName]), metadata=metadata)

        # 最后检查 folder_path 是否为空
        if await is_directory_empty(stub, metadata, folder_path):
            await stub.DeleteFiles(clouddrive_pb2.MultiFileRequest(path=[folder_path]), metadata=metadata)
            return f"🗑️ 清理了 {delete_count} 个小文件，变为空目录已删除: `{folder_name}`"

        return f"🧹 已从 `{folder_name}` 中移除 {delete_count} 个小文件。" if delete_count > 0 else None

    except Exception as e:
        logger.error(f"处理文件夹 {folder_name} 出错: {str(e)}")
        return f"❌ 处理 `{folder_name}` 异常: {str(e)}"


async def run_auto_clean():
    """定时任务调用的主扫描函数"""
    logger.info("⏰ [Schedule] 启动定时自动化清理任务...")
    try:
        async with grpc.aio.insecure_channel(CD2_IP_PORT) as channel:
            stub = clouddrive_pb2_grpc.CloudDriveFileSrvStub(channel)
            metadata = [('authorization', f'Bearer {CD2_TOKEN}')]
            root_req = clouddrive_pb2.ListSubFileRequest(path=SAVE_PATH)

            async for reply in stub.GetSubFiles(root_req, metadata=metadata, timeout=30):
                if reply.subFiles:
                    for f in reply.subFiles:
                        if f.isDirectory:
                            await clean_task_folder(stub, metadata, f.fullPathName)
        logger.info("✅ [Schedule] 自动清理任务执行完毕。")
    except Exception as e:
        logger.error(f"❌ [Schedule] 自动任务运行失败: {str(e)}")


# ==========================================
# 3. Telegram 交互处理器
# ==========================================

def _is_network_error(error: object) -> bool:
    """识别 Telegram/httpx 抛出的可恢复网络异常。"""
    error_text = str(error)
    return (
        isinstance(error, (NetworkError, TimedOut))
        or "ConnectError" in error_text
        or "ConnectTimeout" in error_text
    )


def _record_network_error(now: float | None = None) -> int:
    """记录当前网络异常，并在静默超过配置窗口后开始新一轮计数。"""
    global _network_error_count, _last_network_error_at

    current_time = time.monotonic() if now is None else now
    # 成功请求不会进入错误处理器，因此在下一次异常到来时按静默时长惰性重置。
    if (
        _last_network_error_at is None
        or current_time - _last_network_error_at >= NETWORK_ERROR_RESET_SECONDS
    ):
        _network_error_count = 1
    else:
        _network_error_count += 1

    _last_network_error_at = current_time
    return _network_error_count

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """全局错误拦截器，可恢复网络异常交给 Telegram 轮询机制继续重连。"""
    error = context.error

    if _is_network_error(error):
        error_count = _record_network_error()
        logger.warning(
            "🌐 网络连接异常，本轮第 %d 次；连续 %d 秒无异常后重新计数。"
            "Telegram 轮询将继续自动重连。错误: %s",
            error_count,
            NETWORK_ERROR_RESET_SECONDS,
            error,
        )
        return

    logger.error("⚠️ 机器人运行时捕获到非网络异常: %s", error)


def _get_polling_task(updater: object) -> object | None:
    """取出 Updater 内部持有的轮询协程任务对象。

    python-telegram-bot 把轮询任务存放在私有属性 `__polling_task`，
    按 Python 名称改写规则，外部访问到的名字是 `_Updater__polling_task`。
    这里用 getattr 防御式取值：一旦未来版本调整内部结构，看门狗自身
    不会因为 AttributeError 而崩溃，只会安静地不工作。
    """
    return getattr(updater, "_Updater__polling_task", None)


async def watchdog_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    """轮询看门狗：发现 Telegram 轮询协程已死亡时，主动退出以便容器重启自愈。

    为什么需要它（v1.1.5 未能覆盖的真实故障）：
        PTB 的 network_retry_loop 会把 HTTP 401/404 映射为 InvalidToken，
        而 `except InvalidToken` 分支排在 `except TelegramError` 之前，
        且只记日志后直接 `raise`，不会调用 on_err_cb。
        结果是：轮询协程带着异常结束，但进程仍然存活，
        全局 error_handler 也完全捕获不到任何异常。
        对外表现就是「容器 running、重启次数 0、却再也收不到任何消息」的永久静默。

    本任务周期检查轮询任务的状态：一旦发现它已结束（而应用理应还在运行），
    就记录 CRITICAL 并调用 stop_running() 让进程退出，
    交由 Docker 的 restart: always 完成重启，把「永久静默」降级为「最多静默一个检查周期」。
    """
    application = context.application

    # 应用正在正常关闭时，PTB 自己会取消轮询任务，这种情况不是故障，必须放行
    if not application.running:
        return

    updater = application.updater
    if updater is None:
        return

    polling_task = _get_polling_task(updater)
    # 还没启动轮询，说明应用仍在 bootstrap 阶段，交由 PTB 自身的重试逻辑处理
    if polling_task is None or not polling_task.done():
        return

    if polling_task.cancelled():
        reason = "轮询任务被取消"
    else:
        error = polling_task.exception()
        reason = f"{type(error).__name__}: {error}" if error else "轮询任务已结束（无异常）"

    logger.critical(
        "🛑 检测到 Telegram 轮询已停止：%s。"
        "这通常意味着 401/404 触发了 InvalidToken 并绕过了错误处理器。"
        "现在主动退出进程，交由 Docker restart 策略重启自愈。",
        reason,
    )
    application.stop_running()


# 回执发送的最大尝试次数与退避间隔(秒)。
# Telegram/代理侧的连接中断往往是瞬时的(重建连接即可成功)，所以值得重试；
# 但绝不能把这类故障当成业务故障去误导用户，也不能让它不留痕迹。
REPLY_MAX_ATTEMPTS = 3
REPLY_RETRY_DELAY_SECONDS = 1.5


def _mask_link(link: str, limit: int = 80) -> str:
    """截断超长链接用于日志，避免 magnet 的 dn 参数把日志刷爆。"""
    return link if len(link) <= limit else f"{link[:limit]}…(共 {len(link)} 字符)"


async def _safe_send(send_func, text: str, description: str, **kwargs) -> bool:
    """发送/编辑 Telegram 消息，遇到瞬时网络故障自动重试，且失败必留日志。

    为什么要单独抽出来：
        「提交到 CD2」走 gRPC，「给用户回执」走 Telegram(经代理)，这是两条互不相干的链路。
        旧实现用一个 `except Exception` 把两者包在一起，于是任何发送失败都被写成
        「❌ 提交失败，CD2 连接异常」—— 云盘里任务其实跑得好好的，用户却被误导，
        而日志里连一行记录都没有，完全无法排查。

    返回 True 表示消息最终送达。调用方必须根据返回值决定后续动作，
    不要用「发送失败」去否定已经完成的业务动作。
    """
    last_error: Exception | None = None

    for attempt in range(1, REPLY_MAX_ATTEMPTS + 1):
        try:
            await send_func(text, **kwargs)
            return True
        except Exception as e:
            last_error = e
            # 非网络异常(例如 Markdown 解析失败、消息过长)重试没有意义，立刻放弃
            if not _is_network_error(e):
                logger.error("❌ 【%s】发送失败(非网络异常，不重试): %s", description, e, exc_info=True)
                return False

            if attempt < REPLY_MAX_ATTEMPTS:
                logger.warning(
                    "⚠️ 【%s】发送失败(%d/%d)，%.1f 秒后重试: %s",
                    description,
                    attempt,
                    REPLY_MAX_ATTEMPTS,
                    REPLY_RETRY_DELAY_SECONDS,
                    e,
                )
                await asyncio.sleep(REPLY_RETRY_DELAY_SECONDS)

    logger.error(
        "❌ 【%s】重试 %d 次后仍发送失败，消息未送达用户: %s",
        description,
        REPLY_MAX_ATTEMPTS,
        last_error,
        exc_info=last_error,
    )
    return False


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """监听并处理发送的磁力链接、HTTP、电驴链接"""
    if update.effective_user.id not in ADMIN_IDS: return
    text = update.message.text.strip()

    if not any(text.startswith(p) for p in ["magnet:", "http", "ed2k://"]):
        return

    # ---------- 阶段一：提交到 CD2 ----------
    # 只有这一段失败，才配称之为「CD2 连接异常」，并必须留下完整堆栈。
    try:
        async with grpc.aio.insecure_channel(CD2_IP_PORT) as channel:
            stub = clouddrive_pb2_grpc.CloudDriveFileSrvStub(channel)
            metadata = [('authorization', f'Bearer {CD2_TOKEN}')]
            req = clouddrive_pb2.AddOfflineFileRequest(urls=text, toFolder=SAVE_PATH)
            res = await stub.AddOfflineFiles(req, metadata=metadata, timeout=15)
    except Exception as e:
        logger.exception("❌ 提交 CD2 离线下载失败 [%s]: %s", type(e).__name__, _mask_link(text))
        await _safe_send(
            update.message.reply_text,
            f"❌ 提交失败，CD2 连接异常: {type(e).__name__}: {e}",
            description="CD2 连接异常回执",
        )
        return

    if not res.success:
        logger.warning("⚠️ CD2 拒绝离线下载请求: %s | 链接: %s", res.errorMessage, _mask_link(text))
        await _safe_send(
            update.message.reply_text,
            f"❌ CD2 拒绝请求: {res.errorMessage}",
            description="CD2 拒绝回执",
        )
        return

    # ---------- 阶段二：回执 ----------
    # 走到这里说明任务已经在 CD2 上跑起来了。
    # 后续回执发不出去只能记日志，绝不能反过来告诉用户「提交失败」。
    logger.info("✅ 已提交离线下载: %s", _mask_link(text))
    delivered = await _safe_send(
        update.message.reply_text,
        f"✅ 提交成功！\n📂 目录：`{SAVE_PATH}`\n提示：完成后发送 /clean 执行清理。",
        description="提交成功回执",
    )
    if not delivered:
        logger.error("❗ 任务已在 CD2 提交成功，但成功回执未能送达用户，链接: %s", _mask_link(text))


async def cmd_clean(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """手动清理命令 (/clean)"""
    if update.effective_user.id not in ADMIN_IDS: return
    status_msg = await update.message.reply_text("🔍 正在全量扫描目录，请稍后...")
    results: list[str] = []
    try:
        async with grpc.aio.insecure_channel(CD2_IP_PORT) as channel:
            stub = clouddrive_pb2_grpc.CloudDriveFileSrvStub(channel)
            metadata = [('authorization', f'Bearer {CD2_TOKEN}')]
            root_req = clouddrive_pb2.ListSubFileRequest(path=SAVE_PATH)
            dir_count = 0
            async for reply in stub.GetSubFiles(root_req, metadata=metadata, timeout=30):
                if reply.subFiles:
                    for f in reply.subFiles:
                        if f.isDirectory:
                            dir_count += 1
                            res = await clean_task_folder(stub, metadata, f.fullPathName)
                            if res: results.append(res)
            logger.info(f"📂 SAVE_PATH 下共发现 {dir_count} 个子目录")
    except Exception as e:
        # 这里同样只兜住 CD2 侧的失败，日志要留全，避免「清理失败」变成无迹可查的悬案
        logger.exception("❌ 扫描/清理失败 [%s]: %s", type(e).__name__, e)
        await _safe_send(
            status_msg.edit_text,
            f"❌ 无法执行清理: `{type(e).__name__}: {e}`",
            description="清理失败回执",
            parse_mode='Markdown',
        )
        return

    # 报告发送独立于清理流程：报告发不出去不代表清理失败，反过来也一样
    report = "\n".join(results) if results else "✅ 下载目录非常整洁，无需清理。"
    # 报告里会带文件名，Markdown 特殊字符可能让 Telegram 解析失败，失败时降级为纯文本重发
    if not await _safe_send(
        status_msg.edit_text,
        f"📊 **清理报告：**\n{report}",
        description="清理报告(Markdown)",
        parse_mode='Markdown',
    ):
        await _safe_send(
            status_msg.edit_text,
            f"📊 清理报告：\n{report}",
            description="清理报告(纯文本降级)",
        )


async def cmd_blacklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理黑名单关键词 (/blacklist)"""
    if update.effective_user.id not in ADMIN_IDS: return
    current = get_blacklist()
    if context.args:
        new_word = " ".join(context.args)
        if new_word not in current:
            current.append(new_word)
            with open(BLACKLIST_FILE, "w", encoding="utf-8") as f:
                for k in current: f.write(f"{k}\n")
            await update.message.reply_text(f"➕ 已添加黑名单关键词: `{new_word}`", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"📝 当前黑名单:\n`{', '.join(current)}`", parse_mode='Markdown')


async def post_init(application):
    """
    机器人启动后的初始化:
    - 注册手机端指令菜单。
    - 在运行中的事件循环内启动 Cron 调度器，解决 RuntimeError 问题。
    - 注册轮询看门狗，兜底 Telegram 轮询协程静默死亡的场景。
    """
    await application.bot.set_my_commands([
        BotCommand("clean", "手动扫描下载目录并清理"),
        BotCommand("blacklist", "查看或更新黑名单关键词")
    ])
    # 初始化并启动调度器
    # 修复假死问题：不要单独创建 AsyncIOScheduler 实例，否则会引发 asyncio 事件循环冲突
    # 改为使用 python-telegram-bot 内置的 job_queue，由于自带的 job_queue 可以良好管理协程，避免卡死。
    if application.job_queue:
        # job_queue 内部包含了一个配置好的 apscheduler 实例
        application.job_queue.scheduler.add_job(
            run_auto_clean, 
            CronTrigger.from_crontab(CLEAN_CRON)
        )
        logger.info(f"📅 定时任务系统已启动(基于内置JobQueue)，Cron 设定: [{CLEAN_CRON}]")

        # 轮询看门狗：周期性检查 Telegram 轮询协程是否还活着，
        # 防止 401/404 → InvalidToken 导致的「进程活着但收不到消息」永久静默。
        if WATCHDOG_INTERVAL_SECONDS > 0:
            application.job_queue.run_repeating(
                watchdog_check,
                interval=WATCHDOG_INTERVAL_SECONDS,
                first=WATCHDOG_INTERVAL_SECONDS,
                name="polling_watchdog",
            )
            logger.info(f"🐶 轮询看门狗已启动，检查周期 {WATCHDOG_INTERVAL_SECONDS} 秒。")
        else:
            logger.warning("⚠️ 轮询看门狗已被禁用 (WATCHDOG_INTERVAL_SECONDS=0)。")
    else:
        logger.error("❌ 无法启动定时清理任务：内置的 JobQueue 未初始化。")


# ==========================================
# 4. 程序入口
if __name__ == '__main__':
    # 代理网络配置
    request_kwargs = {
        "connection_pool_size": 8,
        "read_timeout": 30.0,
        "write_timeout": 30.0,
        "connect_timeout": 20.0,
        "pool_timeout": 15.0
    }
    
    if PROXY_URL:
        logger.info(f"正在配置网络代理: {PROXY_URL}")
        # telegram.request.HTTPXRequest 在 v22+ 支持直接传入 proxy 参数
        q_request = HTTPXRequest(proxy=PROXY_URL, **request_kwargs)
        u_request = HTTPXRequest(proxy=PROXY_URL, **request_kwargs)
    else:
        q_request = HTTPXRequest(**request_kwargs)
        u_request = HTTPXRequest(**request_kwargs)
        
    # 构造应用实例，并同时为 bot 实例和 updater(getUpdates轮询) 注入支持代理的网络请求类
    builder = ApplicationBuilder().token(TG_BOT_TOKEN).post_init(post_init).request(q_request).get_updates_request(u_request)

    app = builder.build()

    # 注册异常拦截器
    app.add_error_handler(error_handler)

    # 注册消息与指令处理器
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_link))
    app.add_handler(CommandHandler("clean", cmd_clean))
    app.add_handler(CommandHandler("blacklist", cmd_blacklist))

    logger.info("🚀 CD2 Bot 已启动，正在轮询消息...")
    # python-telegram-bot 的 run_polling 默认在遇到网络错误时会自动重试
    # 通过 error_handler 捕获并记录异常，无需额外配置重试参数
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )
