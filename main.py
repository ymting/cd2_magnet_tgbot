# -*- coding: utf-8 -*-
"""
项目名称: CloudDrive2 Telegram 离线下载管家
版本: 1.1.10-10 (dev 预发布；生产发版时改为 1.1.10)
功能描述:
    1. 链接监听: 自动识别 Magnet、ed2k、http(s) 直链并提交至 CD2 离线下载。
       支持一条消息里混合粘贴多个不同类型的链接，逐个提交后汇总回执。
       注意 http(s) 直链能否真正下载取决于后端网盘（115open 通常只支持磁力 / ed2k）。
    2. 定时清理: 基于 Cron 表达式，递归扫描下载目录，删除小文件和黑名单文件，清理空目录。
    3. 异常容错: 增加全局错误处理与 gRPC 超时控制，防止网络波动导致假死。
    4. 轮询看门狗: 周期检测 Telegram 轮询协程是否已死亡，避免「容器活着但收不到消息」的永久静默。
    5. 故障归因: 提交(CD2/gRPC)与回执(Telegram/httpx)分开处理，回执失败不再误报为「CD2 连接异常」。
    6. 拒绝提示口语化: CD2 的业务拒绝(含重复提交)不再转发技术性报错，改为归类成简短人话。
       覆盖两条路径: gRPC 抛异常(115open 把重复链接报成 INTERNAL)与 res.success=False。
       http(s) 直链被拒时单独给出指导性说明（后端网盘通常不吃直链），并附原文摘要备查。
    7. 回执自愈: 发送连接故障自动重建普通请求池，最终通知在内存中有界补发，不重复创建下载。
作者: ymting
"""

import logging
import os
import re
import time
import grpc
import asyncio
import httpx
import clouddrive_pb2
import clouddrive_pb2_grpc
from datetime import datetime
from typing import NamedTuple
from dataclasses import dataclass
from collections.abc import Callable

# 版本号
# 版本号。唯一来源：CI 直接从这里读取并生成镜像标签（见 docker-publish.yml）。
# 约定：master 上是生产版本（如 1.1.10），dev 分支上带 -n 后缀（如 1.1.10-1、1.1.10-2）。
__version__ = "1.1.10-10"
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import Update, BotCommand
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters
from telegram.request import HTTPXRequest
from telegram.error import BadRequest, Forbidden, InvalidToken, NetworkError, TimedOut, RetryAfter

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


class RequestNotSent(NetworkError):
    """连接池恢复阶段即失败，明确还未发出 Telegram 请求。"""


def _request_failure_cause(error: Exception) -> Exception:
    """PTB 把 httpx 异常包装成 TelegramError；归因优先保留真正的底层类型。"""
    return error.__cause__ or error


def _is_unsent_failure(error: Exception) -> bool:
    if isinstance(error, (BadRequest, Forbidden, InvalidToken)):
        return False
    if isinstance(error, RequestNotSent):
        return True
    cause = _request_failure_cause(error)
    if isinstance(cause, (httpx.ConnectError, httpx.ConnectTimeout,
                          httpx.ProxyError, httpx.PoolTimeout)):
        return True
    # 兼容 PTB 不保留 cause 的错误以及旧版测试替身；只认明确的未发送信号。
    return any(hint in str(error) for hint in
               ("httpx.ConnectError", "httpx.ConnectTimeout", "httpx.ProxyError", "Pool timeout:"))


class ResilientHTTPXRequest(HTTPXRequest):
    """普通 Telegram 请求的连接池自愈，不改变独立的 getUpdates 轮询客户端。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._recovery_lock = asyncio.Lock()
        self._needs_recovery = False
        self._shutdown = False

    async def initialize(self):
        async with self._recovery_lock:
            await super().initialize()
            self._shutdown = False

    async def shutdown(self):
        async with self._recovery_lock:
            self._shutdown = True
            await super().shutdown()

    async def _recover_locked(self):
        # 必须与整个请求共用锁：关闭旧池时不能仍有其它请求在使用它。
        # 只调用 PTB 公共生命周期接口，保留代理、超时及所有原始构造参数。
        await super().shutdown()
        await super().initialize()
        self._needs_recovery = False
        logger.warning("🔄 Telegram 普通发送连接池已重建；接收轮询不受影响。")

    async def do_request(self, *args, **kwargs):
        async with self._recovery_lock:
            if self._shutdown:
                raise RuntimeError("Telegram 发送客户端已关闭")
            if self._needs_recovery:
                try:
                    await self._recover_locked()
                except Exception as error:
                    raise RequestNotSent("Telegram 发送连接池恢复失败，请求未发送") from error
            try:
                return await super().do_request(*args, **kwargs)
            except asyncio.CancelledError:
                # TLS 握手被取消也可能留下连接占位；不阻拦取消，下一次请求先重建。
                self._needs_recovery = True
                raise
            except Exception as error:
                if _is_unsent_failure(error):
                    self._needs_recovery = True
                    try:
                        await self._recover_locked()
                    except Exception:
                        # 恢复失败也保留原始网络异常，下次请求会先再次恢复。
                        logger.exception("❌ Telegram 普通发送连接池重建失败，将在下次请求重试。")
                raise


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
        # 日志留全；报告里只放一行摘要，否则 AioRpcError 的整段 repr 会把 Telegram 消息撑爆
        logger.error("❌ 处理文件夹 %s 出错: %s", folder_name, e, exc_info=True)
        return f"❌ 处理 `{folder_name}` 出错：{_shorten(_grpc_error_raw(e))}"


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
    # BadRequest 是 NetworkError 的子类，但重复坏参数永远不能修复发送。
    if isinstance(error, (BadRequest, Forbidden, InvalidToken)):
        return False
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
REPLY_RATE_LIMIT_WAIT_SECONDS = 5
PENDING_REPLY_MAX_ITEMS = 100
PENDING_REPLY_TTL_SECONDS = 3600
PENDING_REPLY_INTERVAL_SECONDS = 30
PENDING_REPLY_MAX_ATTEMPTS = 120
PENDING_REPLY_BATCH_SIZE = 5


def _mask_link(link: str, limit: int = 80) -> str:
    """截断超长链接用于日志，避免 magnet 的 dn 参数把日志刷爆。"""
    return link if len(link) <= limit else f"{link[:limit]}…(共 {len(link)} 字符)"


# ---------------------------------------------------------------------------
# 链接解析：一条消息里可能同时出现多个不同类型的链接
# ---------------------------------------------------------------------------
# 这里用同一套正则把消息正文里的链接全部抠出来（magnet / ed2k / http(s)），
# 不再要求整条消息以链接开头，因此
# 「1. magnet:... 2. ed2k://... 3. https://...」这类带序号或项目符号的列表也能识别。
# （旧实现用 startswith 判断，遇到序号前缀会整条消息被丢弃，一个都提交不了。）
#
# 关于 http(s)：正则**照常识别**它们（用户确实可能粘贴直链），但能否真正下载取决于
# CD2 背后的网盘 —— 详见 _HTTP_SCHEMES 处的说明，提交失败时会给出指导性文案。

# 中文标点。它们永远不会出现在链接里，却常常紧跟链接出现（「链接：magnet:...。还有 ed2k://...」），
# 必须在这里就截断匹配，否则会把后面的正文一起吞进链接。
# 注意只截断「标点」而不截断汉字：ed2k 的 |file| 文件名段经常直接写中文（未做 URL 编码），
# 若在汉字处断开会把链接截断成失效的半截。
_CJK_PUNCTUATION = "，。、；：！？（）【】《》「」『』“”‘’—…～·"

# 协议名大小写不敏感（用户可能粘贴成 Magnet: / HTTPS://）。
_LINK_PATTERN = re.compile(
    rf"(?:magnet:|ed2k://|https?://)[^\s<>\"'{_CJK_PUNCTUATION}]+",
    re.IGNORECASE,
)

# 从聊天里复制链接时末尾常粘上标点，需要从链接尾部剥掉。
# 刻意不含 ASCII 的 ) ] } —— 它们可能是 URL 本身的组成部分（如维基百科条目名 Foo_(bar)）。
_LINK_TRAILING_CHARS = "，。、；：！？…）】》」』”’.,;:!?"

# 成对包裹符号。链接被 `\``、`[ ]`、`( )` 包住时（从网页或 Markdown 里复制很常见），
# 末尾会粘上闭合符号。判断依据：只有当链接主体里**没有**对应的开启符号时，
# 才认定它是「包裹」而不是 URL 自身的结构 ——
# 这样既剥得掉 [magnet:...] 的 ]，又不会误伤 https://.../Foo_(bar) 和 http://[::1]:8080 的 ) ]。
_WRAPPER_PAIRS = {")": "(", "]": "[", "}": "{", "`": "`"}

# 零宽字符：从网页 / App 复制时会被夹带进来，肉眼不可见但会让 CD2 判定链接非法。
# 它们不是分隔符（不能用来断句），而是噪音，按「直接删除」处理。
_INVISIBLE_CHARS = str.maketrans("", "", "\u200b\u200c\u200d\u2060\ufeff")

# 超链接实体（text_link）里只认这两种协议。
# 为什么只认这两种：转发来的消息里 http(s) 超链接多半是频道、群组、广告，
# 把它们当下载链接提交只会给用户刷一串失败提示；而 magnet / ed2k 一定是下载意图。
_LINK_ENTITY_SCHEMES = ("magnet:", "ed2k://")

# 去掉协议头后至少还要有这么多字符才算一个「像样的」链接，
# 用来挡掉正文里出现的裸 "https://" 之类的碎片。
_LINK_MIN_BODY_CHARS = 3

# 单个链接在批量报告里回显的最大长度（复用 _mask_link 的截断格式）
_BATCH_LINK_LABEL_LIMIT = 40

# 批量报告的整体长度上限。Telegram 单条消息上限 4096 字符，超限会抛 BadRequest，
# 而 BadRequest 属于非网络异常（_safe_send 不重试）→ 用户最终什么都收不到。
# 所以这里主动截断成「只展示前 N 条」，而不是把整份报告发出去撞墙。
_BATCH_REPORT_LIMIT = 3500


def _clean_link_tail(link: str, scheme_end: int) -> str:
    """剥掉链接尾部粘上的标点与成对包裹符号。

    两种情况会叠加（例如 `[magnet:...]。`：先掉句号，再掉方括号），
    因此循环到不再变化为止。
    """
    while True:
        cleaned = link.rstrip(_LINK_TRAILING_CHARS)
        while len(cleaned) - scheme_end > 1:
            closer = cleaned[-1]
            opener = _WRAPPER_PAIRS.get(closer)
            if opener is None:
                break
            # 末尾可能叠着多个同种闭合符号（三反引号代码块紧贴链接时会这样）
            run = len(cleaned) - len(cleaned.rstrip(closer))
            if closer == opener:
                # 同字符配对（反引号）：奇数个里必然有一个是孤立的，整段剥掉
                if run % 2 == 0:
                    break
            elif opener in cleaned[scheme_end:-run]:
                # 不同字符配对：主体里已有开启符号 → 属于 URL 自身结构（Foo_(bar) / [::1]）
                break
            cleaned = cleaned[:-run]
        if cleaned == link:
            return cleaned
        link = cleaned


def _normalize_links(candidates) -> list[str]:
    """把原始候选串清洗成可提交的链接，按顺序去重后返回。

    明文解析与超链接实体两条来源共用这一层，保证清洗规则不会两边不一致。
    处理内容：协议名统一小写、删零宽字符、剥尾部标点与包裹符号、挡掉没有正文的碎片。
    """
    links: list[str] = []
    seen: set[str] = set()

    for raw in candidates:
        if not raw:
            continue
        # 协议名统一小写（用户可能手打成 Magnet: / HTTPS://），其后的内容原样保留 ——
        # 对整串 lower() 会改掉 magnet 里 dn 显示名等参数的大小写。
        scheme_end = raw.find(":") + 1
        if scheme_end == 0:  # 连协议名都没有，不是链接
            continue
        link = (raw[:scheme_end].lower() + raw[scheme_end:]).translate(_INVISIBLE_CHARS)
        link = _clean_link_tail(link, scheme_end)

        if len(link) - scheme_end < _LINK_MIN_BODY_CHARS or link in seen:
            continue
        seen.add(link)
        links.append(link)

    return links


def _extract_links(text: str | None) -> list[str]:
    """从一段纯文本里提取所有链接，按出现顺序返回，并去掉完全重复的条目。

    链接之间用换行、空格、制表符还是全角空格分隔都能识别 —— 正则按空白切分，
    所以「一行一个链接」这种最常见的粘贴方式天然被覆盖。

    为什么要去重：同一条消息里重复粘贴同一个链接时，逐个提交必然全部被 CD2 判为重复，
    真正有价值的信息是「其它链接有没有提交成功」，没必要让重复项占满报告。
    """
    return _normalize_links(match.group(0) for match in _LINK_PATTERN.finditer(text or ""))


def _collect_message_links(message) -> list[str]:
    """汇总一条消息里所有可提交的链接，兼容转发的各种形态。

    三种来源，缺一不可：
      1. `text` —— 纯文本消息（转发不会改写正文，所以转发来的链接照样能提取）；
      2. `caption` —— **媒体帖的说明文字**，转发种子 / 资源帖最常见就是这个形态，
         此时 `text` 为 None，旧实现只读 `text`，对这类转发完全不响应；
      3. `text_link` 超链接实体 —— 正文里只有「点此下载」几个字，真实地址在
         `entity.url` 里，纯文本解析永远看不到（只取 magnet / ed2k，理由见 `_LINK_ENTITY_SCHEMES`）。

    两种来源合并后统一去重，避免同一条链接被提交两次。
    """
    text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
    candidates = [match.group(0) for match in _LINK_PATTERN.finditer(text)]

    # 文字实体挂在 text 上，说明文字的实体挂在 caption 上，两个字段都要看
    for field in ("entities", "caption_entities"):
        for entity in getattr(message, field, None) or []:
            url = getattr(entity, "url", None)
            if url and url.lower().startswith(_LINK_ENTITY_SCHEMES):
                candidates.append(url)

    return _normalize_links(candidates)


# ---------------------------------------------------------------------------
# 失败原因「人话化」文案
# ---------------------------------------------------------------------------
# 为什么需要这一层：
#   CD2 的技术性报错会从**两条路**到达用户，两条都必须拦：
#     (a) gRPC 直接抛异常 —— 云盘侧的拒绝被 CD2 包成 gRPC 错误码抛出。
#         实测 115open 对重复链接返回的是 StatusCode.INTERNAL + code 10008 +
#         「任务已存在，请勿输入重复的链接地址」，而不是 success=False。
#         这条路的异常 repr 有 4 行（status / details / debug_error_string），
#         且 details 与 debug_error_string 内容重复，直接展示既长又难读。
#     (b) res.success == False + errorMessage —— 面向开发者的描述，常夹带
#         云盘 API 原文、错误码、JSON。
#   两条路统一按关键词归类成简短提示；完整原文一律只写进日志备查。
DUPLICATE_REPLY = "⚠️ 这个链接之前已经提交过了，无需重复提交。"

# 全部小写保存，比较时统一对报错文本做 lower()，中英文关键词即可共用一套判断。
_DUPLICATE_HINTS = (
    "已存在", "已经存在", "已在", "已添加", "已经添加", "已提交", "已经提交",
    "已收录", "已下载", "已离线", "重复",
    "already exist", "already add", "already been", "already in", "has been added",
    "duplicate", "task exist", "task already", "in the list",
)
_AUTH_HINTS = (
    "token", "unauthenticated", "unauthorized", "permission denied",
    "invalid credential", "认证", "授权", "无权限",
)
_UNSUPPORTED_HINTS = (
    "not support", "unsupported", "invalid url", "不支持", "无法解析",
)

# HTTP/HTTPS 直链。CD2 的离线下载是由**后端网盘**执行的（115open / 迅雷 / PikPak），
# 而 115open 这类网盘通常只吃磁力 / ed2k，直链基本会被拒。
# 用户很可能因为 README 的旧承诺（「支持 http://」）而粘贴直链，
# 所以这类失败必须给一句有指导性的说明，而不是笼统的「不支持这个链接」。
_HTTP_SCHEMES = ("http://", "https://")
_HTTP_DIRECT_LINK_REASON = (
    "HTTP/HTTPS 直链通常无法通过 CD2 离线下载"
    "（CD2 由后端网盘执行下载，115open 之类的网盘一般只支持磁力 / ed2k）。"
    "请改用磁力或 ed2k 链接。"
)


def _is_http_link(link: str | None) -> bool:
    """判断是否为 http/https 直链（大小写不敏感）。"""
    return bool(link) and link.lower().startswith(_HTTP_SCHEMES)


def _http_direct_link_reply(detail: str = "") -> str:
    """http(s) 直链提交失败时的专用文案。

    detail 非空时附一行「原始原因」摘要（已单行化 + 截断）：结论在前、证据在后，
    既说清楚「为什么不行」，又不会把云盘 API 原文整段甩给用户。
    """
    reply = f"❌ 提交失败：{_HTTP_DIRECT_LINK_REASON}"
    if detail:
        reply += f"\n原始原因：{_shorten(detail)}"
    return reply

# 剥掉 CD2 常见的动作前缀，避免「添加离线下载任务失败: xxx」这类无信息量的重复措辞占满屏幕
_REJECT_PREFIXES = (
    "添加离线下载任务失败:", "添加离线任务失败:", "添加离线文件失败:",
    "离线下载失败:", "添加任务失败:",
    "add offline file failed:", "failed to add offline file:", "addofflinefiles failed:",
)

# 未归类错误最多展示的字符数，超出部分截断（完整原文仍在日志里）
_REJECT_TEXT_LIMIT = 80

# 只有这些 gRPC 状态码才是「真的连不上 CD2」，才允许说「CD2 连接异常」。
# 特别注意：INTERNAL 不算 —— 云盘 API 的业务拒绝（如 115open 的 10008 重复任务）
# 也是以 INTERNAL 抛出的，把它说成「连接异常」正是上一版残留的误报。
_TRANSPORT_STATUS_CODES = ("StatusCode.UNAVAILABLE", "StatusCode.DEADLINE_EXCEEDED")
_TRANSPORT_TEXT_HINTS = (
    "unavailable", "deadline exceeded", "connection refused",
    "failed to connect", "no route to host", "timed out", "connect failed",
)


def _shorten(text: str, limit: int = _REJECT_TEXT_LIMIT) -> str:
    """把一段文本压成单行并截断，避免多行技术描述或超长报文甩给用户。

    先做单行化（把换行/连续空格归一）再截断 —— AioRpcError 的 repr 是 4 行，
    不压行的话 Telegram 里会出现大段堆栈样式的内容。
    """
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _grpc_error_raw(error: BaseException) -> str:
    """取出异常里最有价值的一段文本，优先 AioRpcError.details()。

    AioRpcError 的 str() 包含 status / details / debug_error_string 三段，
    而 debug_error_string 与 details 基本是同一份内容，展示价值为零。
    details() 恰好只给业务原因（例如「code: 10008, message: 任务已存在…」），
    既短又能直接用于归类。
    """
    details = getattr(error, "details", None)
    if callable(details):
        try:
            text = (details() or "").strip()
            if text:
                return text
        except Exception:
            # 某些异常对象的 details 可能抛错，静默退回 str()，不能因此再抛一层
            pass
    return str(error).strip()


def _is_transport_failure(error: BaseException) -> bool:
    """判断异常是否属于「连不上 CD2」这类真正的传输层故障。"""
    code = getattr(error, "code", None)
    if callable(code):
        try:
            return str(code()) in _TRANSPORT_STATUS_CODES
        except Exception:
            pass
    lowered = str(error).lower()
    return any(hint in lowered for hint in _TRANSPORT_TEXT_HINTS)


def _classify_reject_reason(raw_message: str, link: str | None = None) -> str | None:
    """识别报错文本属于哪类已知拒绝；识别不出返回 None，由调用方兜底。

    单独抽出来是为了让两条路径共用同一套判断：
    gRPC 异常走 _describe_submit_failure()，success=False 走 _friendly_reject_reason()。

    link 只参与「不支持」这一类的措辞：http(s) 直链被拒几乎总是因为后端网盘不吃直链，
    单独给一句指导性文案；其余类别与报错文本强相关，不依赖 link。
    """
    text = (raw_message or "").strip()
    if not text:
        return None
    lowered = text.lower()

    if any(hint in lowered for hint in _DUPLICATE_HINTS):
        return DUPLICATE_REPLY
    if any(hint in lowered for hint in _AUTH_HINTS):
        return "❌ 提交失败：CD2 授权失效，请检查 CD2_TOKEN 是否有效。"
    if any(hint in lowered for hint in _UNSUPPORTED_HINTS):
        if _is_http_link(link):
            return _http_direct_link_reply()
        return "❌ 提交失败：CD2 不支持这个链接（格式无法解析或网盘不支持离线下载）。"
    return None


def _friendly_reject_reason(raw_message: str, link: str | None = None) -> str:
    """路径 (b)：res.success=False 时，把 errorMessage 转成用户看得懂的一句话。

    识别不出类别的错误只做「去前缀 + 截断」，不会丢信息 —— 原文完整记录在日志中。
    """
    classified = _classify_reject_reason(raw_message, link)
    if classified:
        return classified

    text = (raw_message or "").strip()
    if not text:
        # CD2 只回了 success=false 却没给原因，不能让用户对着空白猜
        if _is_http_link(link):
            return _http_direct_link_reply()
        return "❌ 提交失败：CD2 未说明原因，请稍后在 CloudDrive2 中确认任务状态。"

    # http(s) 直链的失败原文五花八门（网盘不吃直链 / 格式不认 / 资源失效…），
    # 归不到具体类别时统一给「直链不受支持」的指导性结论，并附原文摘要备查。
    if _is_http_link(link):
        return _http_direct_link_reply(text)

    lowered = text.lower()
    for prefix in _REJECT_PREFIXES:
        if lowered.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    return f"❌ 提交失败：{_shorten(text)}"


def _describe_submit_failure(error: BaseException, link: str | None = None) -> str:
    """路径 (a)：提交阶段抛异常时，给用户看的一句话。

    分四种情况，顺序不能换：
      1. 能归类出业务原因（重复提交 / 授权 / 不支持）→ 说人话，**不提「连接异常」**；
      2. 真正的传输层故障（UNAVAILABLE / DEADLINE_EXCEEDED）→ 才说「CD2 连接异常」。
         这一步必须排在 http 直链判断**之前** —— 否则真的连不上 CD2 时，
         会被误报成「云盘不支持直链」，把最该排查的网络问题藏起来；
      3. http(s) 直链的其它失败 → 归因到「直链不受支持」并附原文摘要；
      4. 其余未知错误 → 如实说失败 + 异常类型名，同样不误导成连接问题。
    """
    raw = _grpc_error_raw(error)

    classified = _classify_reject_reason(raw, link)
    if classified:
        return classified

    if _is_transport_failure(error):
        return f"❌ 提交失败，CD2 连接异常（{type(error).__name__}）：{_shorten(raw)}"

    if _is_http_link(link):
        return _http_direct_link_reply(raw)

    if not raw:
        return "❌ 提交失败：CD2 未说明原因，请稍后在 CloudDrive2 中确认任务状态。"
    return f"❌ 提交失败（{type(error).__name__}）：{_shorten(raw)}"


@dataclass
class PendingReply:
    """只保存原通知，不保存下载动作；进程退出后内存队列随之丢失。"""

    send_func: Callable
    text: str
    description: str
    kwargs: dict
    expires_at: float
    next_attempt_at: float
    attempts: int = 0
    is_edit: bool = False


_pending_replies: list[PendingReply] = []
_pending_flush_running = False
_pending_active_reply: PendingReply | None = None


def _retry_after_seconds(error: RetryAfter) -> float:
    delay = error.retry_after
    return max(0.0, delay.total_seconds() if hasattr(delay, "total_seconds") else float(delay))


def _queue_pending_reply(send_func, text, description, kwargs, *, delay=0.0, is_edit=False):
    now = time.monotonic()
    _pending_replies[:] = [item for item in _pending_replies
                           if item.expires_at > now or item is _pending_active_reply]
    # 同一条原消息、同一份通知不能重复入队；不同用户消息仍各有自己的回执。
    for item in _pending_replies:
        if item.send_func == send_func and item.text == text and item.kwargs == kwargs:
            item.next_attempt_at = max(item.next_attempt_at, now + delay)
            return True
    if len(_pending_replies) >= PENDING_REPLY_MAX_ITEMS or delay >= PENDING_REPLY_TTL_SECONDS:
        logger.error("❌ 【%s】待发队列已满或等待超过保留期限，通知未入队。", description)
        return False
    _pending_replies.append(PendingReply(
        send_func, text, description, dict(kwargs), now + PENDING_REPLY_TTL_SECONDS,
        now + max(PENDING_REPLY_INTERVAL_SECONDS, delay), is_edit=is_edit,
    ))
    logger.warning("📨 【%s】通知进入待发队列（共 %d 条），只补通知，不重新提交下载。",
                   description, len(_pending_replies))
    return True


async def flush_pending_replies(context):
    """每轮最多补少量通知，每条只尝试一次，避免网络离线时长时间拖住发送通道。"""
    global _pending_flush_running, _pending_active_reply
    if _pending_flush_running or not context.application.running:
        return
    _pending_flush_running = True
    try:
        now = time.monotonic()
        for item in list(_pending_replies):
            if item.expires_at <= now or item.attempts >= PENDING_REPLY_MAX_ATTEMPTS:
                _pending_replies.remove(item)
                logger.error("❌ 【%s】通知补发已到期限/次数上限，停止补发。", item.description)
        due = [item for item in _pending_replies if item.next_attempt_at <= now][:PENDING_REPLY_BATCH_SIZE]
        for item in due:
            if not context.application.running:
                break
            # 前一条发送时其它 handler 可能已经清理过期项，快照不代表当前仍有效。
            if not any(queued is item for queued in _pending_replies):
                continue
            # 前一条 await 期间可能收到更长的 RetryAfter，不能只依赖旧的到期快照。
            if item.next_attempt_at > time.monotonic():
                continue
            if item.expires_at <= time.monotonic():
                _pending_replies.remove(item)
                logger.error("❌ 【%s】通知超过保留期限，停止补发。", item.description)
                continue
            # 发送期间保留队列中的占位，既计入容量，也让前台重入同通知能正确去重。
            _pending_active_reply = item
            item.attempts += 1
            try:
                await item.send_func(item.text, **item.kwargs)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if item.is_edit and isinstance(error, BadRequest) and "message is not modified" in str(error).lower():
                    _pending_replies.remove(item)
                    logger.info("✅ 【%s】目标消息已是最终内容，补发完成。", item.description)
                    continue
                if isinstance(error, RetryAfter):
                    delay = _retry_after_seconds(error)
                elif _is_network_error(error) and (item.is_edit or _is_unsent_failure(error)):
                    delay = min(300.0, PENDING_REPLY_INTERVAL_SECONDS * 2 ** min(item.attempts, 4))
                else:
                    _pending_replies.remove(item)
                    logger.error("❌ 【%s】补发终止（永久错误或送达不确定，避免重复通知）: %s",
                                 item.description, error, exc_info=True)
                    continue
                # 发送期间若前台重复入队带来更长的限流等待，不能被本次较短退避覆盖。
                item.next_attempt_at = max(item.next_attempt_at,
                                           time.monotonic() + max(PENDING_REPLY_INTERVAL_SECONDS, delay))
                if item.next_attempt_at < item.expires_at and item.attempts < PENDING_REPLY_MAX_ATTEMPTS:
                    _pending_replies.remove(item)
                    _pending_replies.append(item)
                    logger.warning("⚠️ 【%s】第 %d 次补发失败，将按退避间隔继续: %s",
                                   item.description, item.attempts, error)
                else:
                    _pending_replies.remove(item)
                    logger.error("❌ 【%s】通知补发已到期限/次数上限，停止补发。", item.description)
            else:
                _pending_replies.remove(item)
                logger.info("✅ 【%s】通知补发成功。", item.description)
            finally:
                _pending_active_reply = None
    finally:
        _pending_active_reply = None
        _pending_flush_running = False


async def _safe_send(send_func, text: str, description: str, *, queue_on_failure=False,
                     is_edit=False, failure_state=None, **kwargs):
    """发送/编辑 Telegram 消息，遇到瞬时网络故障自动重试，且失败必留日志。

    为什么要单独抽出来：
        「提交到 CD2」走 gRPC，「给用户回执」走 Telegram(经代理)，这是两条互不相干的链路。
        旧实现用一个 `except Exception` 把两者包在一起，于是任何发送失败都被写成
        「❌ 提交失败，CD2 连接异常」—— 云盘里任务其实跑得好好的，用户却被误导，
        而日志里连一行记录都没有，完全无法排查。

    返回值：成功时返回发送结果对象（Message / True），失败返回 None。
        布尔语义与旧版一致（非 None 即送达），但需要拿到消息对象的调用方
        （例如批量提交要先把进度消息 edit 成最终报告）可以直接使用返回值。
        调用方必须根据返回值决定后续动作，不要用「发送失败」去否定已完成的业务动作。
    """
    last_error: Exception | None = None
    if failure_state is not None:
        failure_state.clear()

    def defer(delay=0.0):
        if queue_on_failure:
            queued = _queue_pending_reply(send_func, text, description, kwargs,
                                           delay=delay, is_edit=is_edit)
            if failure_state is not None:
                failure_state["queued"] = queued

    for attempt in range(1, REPLY_MAX_ATTEMPTS + 1):
        try:
            result = await send_func(text, **kwargs)
            # 统一成「成功必返回真值」：PTB 正常会返回 Message，但个别接口/替身可能返回 None，
            # 若原样透传，调用方的 `if not await _safe_send(...)` 会把成功误判成失败。
            return result if result is not None else True
        except Exception as e:
            last_error = e
            uncertain = _is_network_error(e) and not _is_unsent_failure(e)
            if failure_state is not None and uncertain:
                failure_state["uncertain"] = True
            if is_edit and isinstance(e, BadRequest) and "message is not modified" in str(e).lower():
                return True
            if isinstance(e, RetryAfter):
                delay = _retry_after_seconds(e)
                if failure_state is not None:
                    failure_state["rate_limited"] = True
                if attempt < REPLY_MAX_ATTEMPTS and delay <= REPLY_RATE_LIMIT_WAIT_SECONDS:
                    await asyncio.sleep(delay)
                    continue
                defer(delay)
                logger.warning("⚠️ 【%s】Telegram 限流，至少 %.1f 秒后才允许重试。", description, delay)
                return None
            # 非网络异常(例如 Markdown 解析失败、消息过长)重试没有意义，立刻放弃
            if not _is_network_error(e):
                logger.error("❌ 【%s】发送失败(非网络异常，不重试): %s", description, e, exc_info=True)
                return None

            if uncertain and not is_edit:
                # Telegram 可能已接收但响应丢失：新消息重发会重复通知，必须如实记录边界。
                logger.error("❓ 【%s】发送后未确认送达，不自动重发新消息，避免重复通知: %s",
                             description, e, exc_info=True)
                return None

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
        "❌ 【%s】重试 %d 次后仍未确认回执送达: %s",
        description,
        REPLY_MAX_ATTEMPTS,
        last_error,
        exc_info=last_error,
    )
    defer()
    return None


class SubmitOutcome(NamedTuple):
    """单个链接的提交结果。

    ok:          是否提交成功；
    message:     面向用户的一句话（失败时已归类成人话，不含技术性原文）；
    description: 回执日志里的用途标签，用于在日志中区分是哪一类回执。
    """

    ok: bool
    message: str
    description: str


async def _submit_offline_link(stub, metadata, link: str) -> SubmitOutcome:
    """把单个链接提交到 CD2 离线下载，返回可展示给用户的结果。

    这里只负责「提交」，不负责回执 —— 回执由调用方走 _safe_send，
    遵守「提交与回执分开归因」的约定，避免回执失败被误报成 CD2 故障。

    批量提交时所有链接共用同一个 stub，避免为每个链接重复建连。
    """
    try:
        req = clouddrive_pb2.AddOfflineFileRequest(urls=link, toFolder=SAVE_PATH)
        res = await stub.AddOfflineFiles(req, metadata=metadata, timeout=15)
    except Exception as e:
        # 日志留全；用户侧只说人话 —— 云盘的业务拒绝(如 115open 的「任务已存在」)
        # 也是以 gRPC 异常抛出的，直接展示 AioRpcError 的 4 行 repr 毫无意义。
        logger.exception("❌ 提交 CD2 离线下载失败 [%s]: %s", type(e).__name__, _mask_link(link))
        return SubmitOutcome(False, _describe_submit_failure(e, link), "CD2 提交失败回执")

    if not res.success:
        # 原始 errorMessage 只进日志：它是面向开发者的描述，原样转发给用户既看不懂也容易吓人。
        logger.warning("⚠️ CD2 拒绝离线下载请求: %s | 链接: %s", res.errorMessage, _mask_link(link))
        return SubmitOutcome(False, _friendly_reject_reason(res.errorMessage, link), "CD2 拒绝回执")

    logger.info("✅ 已提交离线下载: %s", _mask_link(link))
    return SubmitOutcome(True, "✅ 提交成功", "提交成功回执")


def _build_batch_report(results: list[tuple[str, SubmitOutcome]]) -> str:
    """把逐条提交结果拼成一份纯文本报告。

    刻意不使用 Markdown：报告里要回显链接原文，而链接中的 `_` `*` `[` 会破坏
    Telegram 的 Markdown 解析，导致整条报告发送失败（项目历史上踩过同类坑）。
    纯文本没有这个问题，也就不需要转义。
    """
    total = len(results)
    success = sum(1 for _, outcome in results if outcome.ok)
    lines = [
        f"📊 批量提交完成：成功 {success} / 失败 {total - success}（共 {total} 个链接）",
        f"📂 目录：{SAVE_PATH}",
        "",
    ]

    shown = 0
    for index, (link, outcome) in enumerate(results, 1):
        line = f"{index}. {'✅' if outcome.ok else '❌'} {_mask_link(link, _BATCH_LINK_LABEL_LIMIT)}"
        if not outcome.ok:
            # 失败才附带原因；文案已经过归类，不会出现技术性原文
            line += f"\n      ↳ {outcome.message}"
        if len("\n".join(lines + [line])) > _BATCH_REPORT_LIMIT:
            break
        lines.append(line)
        shown = index

    omitted = total - shown
    if omitted:
        lines.append(f"…（其余 {omitted} 条结果因消息过长已省略，完整记录见容器日志）")
    else:
        lines.extend(["", "提示：完成后发送 /clean 执行清理。"])
    return "\n".join(lines)


# 处理器的消息过滤器。抽成模块级常量有两个原因：
#   1. 必须带上 CAPTION —— 媒体帖（转发种子/资源帖的常见形态）正文在 caption 里、
#      text 为 None，只写 filters.TEXT 的话这类转发会被过滤器直接挡掉，机器人毫无反应；
#   2. 抽出来才能被单元测试直接断言（见 tests/test_batch_links.py 的 MessageFilterTests）。
LINK_MESSAGE_FILTER = (filters.TEXT | filters.CAPTION) & ~filters.COMMAND


async def _reply_single_link(update: Update, link: str) -> None:
    """单链接路径：保持原有的回执文案（成功时带目录与 /clean 提示）。"""
    try:
        async with grpc.aio.insecure_channel(CD2_IP_PORT) as channel:
            stub = clouddrive_pb2_grpc.CloudDriveFileSrvStub(channel)
            metadata = [('authorization', f'Bearer {CD2_TOKEN}')]
            outcome = await _submit_offline_link(stub, metadata, link)
    except Exception as e:
        # 建连阶段就失败（例如 CD2_ADDRESS 填错导致地址非法），此时单条归类无从产生
        logger.exception("❌ 提交 CD2 离线下载失败 [%s]: %s", type(e).__name__, _mask_link(link))
        outcome = SubmitOutcome(False, _describe_submit_failure(e, link), "CD2 提交失败回执")

    if not outcome.ok:
        await _safe_send(update.message.reply_text, outcome.message, description=outcome.description,
                         queue_on_failure=True)
        return

    # 走到这里说明任务已经在 CD2 上跑起来了。
    # 后续只重试通知、必要时入待发队列，绝不能重新提交或反过来说「提交失败」。
    failure_state = {}
    delivered = await _safe_send(
        update.message.reply_text,
        f"✅ 提交成功！\n📂 目录：`{SAVE_PATH}`\n提示：完成后发送 /clean 执行清理。",
        description="提交成功回执",
        queue_on_failure=True,
        failure_state=failure_state,
    )
    if not delivered:
        reply_status = "暂未确认送达用户" if failure_state.get("uncertain") else "未能送达用户"
        logger.error("❗ 任务已在 CD2 提交成功，但成功回执%s，链接: %s", reply_status, _mask_link(link))


async def _submit_batch_links(update: Update, links: list[str]) -> None:
    """批量路径：先回一条进度提示，再逐个提交，最后把结果汇总编辑进同一条消息。"""
    # 批量提交要逐个走 gRPC，先给用户一个「已收到」的确认，避免看着像没反应
    status_msg = await _safe_send(
        update.message.reply_text,
        f"📥 收到 {len(links)} 个链接，正在逐个提交，请稍候…",
        description="批量提交进度回执",
    )

    results: list[tuple[str, SubmitOutcome]] = []
    try:
        async with grpc.aio.insecure_channel(CD2_IP_PORT) as channel:
            stub = clouddrive_pb2_grpc.CloudDriveFileSrvStub(channel)
            metadata = [('authorization', f'Bearer {CD2_TOKEN}')]
            for link in links:
                results.append((link, await _submit_offline_link(stub, metadata, link)))
    except Exception as e:
        # 只有建连/通道层面的异常会走到这里（单条提交的异常已在 _submit_offline_link 内部归类）。
        # 已提交的链接结果必须保留，未轮到的按同一条原因批量标注，不能整批丢弃。
        logger.exception("❌ 批量提交提前中断 [%s]", type(e).__name__)
        # 这里**刻意不传 link**：中断影响的是「尚未轮到的整批链接」，它们协议可能混杂，
        # 用单条链接的上下文（如「http 直链不受支持」）会给出对整个批次都错误的结论。
        reason = _describe_submit_failure(e)
        results.extend(
            (link, SubmitOutcome(False, reason, "批量提交中断回执"))
            for link in links[len(results):]
        )

    report = _build_batch_report(results)
    # 优先把进度消息改成结果，避免聊天里留一条永远「正在提交…」的提示
    if status_msg is not None:
        failure_state = {}
        if await _safe_send(status_msg.edit_text, report, description="批量提交报告",
                            is_edit=True, queue_on_failure=True, failure_state=failure_state):
            return
        if failure_state.get("queued") or failure_state.get("uncertain") or failure_state.get("rate_limited"):
            # 编辑超时可能已生效，继续编辑同一条消息是幂等的，不能立刻另发造成双报告。
            return
        logger.warning("⚠️ 批量报告编辑失败，改为新消息重发")
    await _safe_send(update.message.reply_text, report, description="批量提交报告(新消息)",
                     queue_on_failure=True)


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """监听并处理消息中的下载链接。

    支持一条消息里混合出现多个不同类型的链接（magnet / ed2k / http(s)）：
    逐个提交后汇总成一份报告；只有一个链接时沿用原有的单条回执文案。

    能覆盖的转发形态见 `_collect_message_links` —— 明文、媒体帖说明文字（caption）、
    以及超链接形式的磁力/ed2k。
    """
    if update.effective_user.id not in ADMIN_IDS: return

    links = _collect_message_links(update.message)
    if not links:
        return

    if len(links) == 1:
        await _reply_single_link(update, links[0])
        return

    await _submit_batch_links(update, links)


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
        # 这里同样只兜住 CD2 侧的失败，日志要留全，避免「清理失败」变成无迹可查的悬案；
        # 用户侧只给一行摘要，且不再用 Markdown（异常文本里的 _ * ` 会让解析失败）
        logger.exception("❌ 扫描/清理失败 [%s]: %s", type(e).__name__, e)
        await _safe_send(
            status_msg.edit_text,
            f"❌ 无法执行清理（{type(e).__name__}）：{_shorten(_grpc_error_raw(e))}",
            description="清理失败回执",
            is_edit=True,
        )
        return

    # 报告发送独立于清理流程：报告发不出去不代表清理失败，反过来也一样
    report = "\n".join(results) if results else "✅ 下载目录非常整洁，无需清理。"
    # 报告里会带文件名，Markdown 特殊字符可能让 Telegram 解析失败，失败时降级为纯文本重发
    if not await _safe_send(
        status_msg.edit_text,
        f"📊 **清理报告：**\n{report}",
        description="清理报告(Markdown)",
        is_edit=True,
        parse_mode='Markdown',
    ):
        await _safe_send(
            status_msg.edit_text,
            f"📊 清理报告：\n{report}",
            description="清理报告(纯文本降级)",
            is_edit=True,
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
        application.job_queue.run_repeating(
            flush_pending_replies,
            interval=PENDING_REPLY_INTERVAL_SECONDS,
            first=PENDING_REPLY_INTERVAL_SECONDS,
            name="pending_reply_delivery",
        )
        logger.info("📨 下载结果通知补发已启用：内存最多 %d 条，保留 %d 秒。",
                    PENDING_REPLY_MAX_ITEMS, PENDING_REPLY_TTL_SECONDS)
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
        q_request = ResilientHTTPXRequest(proxy=PROXY_URL, **request_kwargs)
        u_request = HTTPXRequest(proxy=PROXY_URL, **request_kwargs)
    else:
        q_request = ResilientHTTPXRequest(**request_kwargs)
        u_request = HTTPXRequest(**request_kwargs)
        
    # 构造应用实例，并同时为 bot 实例和 updater(getUpdates轮询) 注入支持代理的网络请求类
    builder = ApplicationBuilder().token(TG_BOT_TOKEN).post_init(post_init).request(q_request).get_updates_request(u_request)

    app = builder.build()

    # 注册异常拦截器
    app.add_error_handler(error_handler)

    # 注册消息与指令处理器
    # 链接处理器同时吃 text 与 caption：转发来的媒体帖正文在 caption 里（见 LINK_MESSAGE_FILTER）
    app.add_handler(MessageHandler(LINK_MESSAGE_FILTER, handle_link))
    app.add_handler(CommandHandler("clean", cmd_clean))
    app.add_handler(CommandHandler("blacklist", cmd_blacklist))

    logger.info("🚀 CD2 Bot 已启动，正在轮询消息...")
    # python-telegram-bot 的 run_polling 默认在遇到网络错误时会自动重试
    # 通过 error_handler 捕获并记录异常，无需额外配置重试参数
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )
