# -*- coding: utf-8 -*-
"""
项目名称: CloudDrive2 Telegram 离线下载管家
功能描述:
    1. 链接监听: 自动识别 Magnet、HTTP、ed2k 链接并提交至 CD2 离线下载。
    2. 定时清理: 基于 Cron 表达式，定时执行下载目录的深度扫描与广告清理。
    3. 异常容错: 增加全局错误处理与 gRPC 超时控制，防止网络波动导致假死。
作者: ymting
"""

import logging
import os
import grpc
import asyncio
import clouddrive_pb2
import clouddrive_pb2_grpc
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import Update, BotCommand
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters
from telegram.request import HTTPXRequest

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

# 配置日志输出，方便在 Docker 日志中查看运行状态
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


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


async def clean_task_folder(stub, metadata, folder_path) -> str | None:
    """
    对单个任务文件夹执行清理动作:
    - 删除匹配关键词的广告文件。
    - 若文件夹为空，或最大文件体积未达标，则整体删除任务。
    """
    folder_name = os.path.basename(folder_path)
    try:
        # 扫描子文件，设置超时防止因 CD2 挂载点卡顿导致程序假死
        req = clouddrive_pb2.ListSubFileRequest(path=folder_path)
        sub_files = []
        async for reply in stub.GetSubFiles(req, metadata=metadata, timeout=15):
            if reply.subFiles: sub_files.extend(reply.subFiles)

        # 场景 A: 空文件夹直接移除
        if not sub_files:
            await stub.DeleteFiles(clouddrive_pb2.MultiFileRequest(path=[folder_path]), metadata=metadata)
            return f"🗑️ 发现空目录已删除: `{folder_name}`"

        # 场景 B: 匹配黑名单关键词
        current_black = get_blacklist()
        files_to_delete = [f.fullPathName for f in sub_files if any(k.lower() in f.name.lower() for k in current_black)]
        delete_count = len(files_to_delete)

        if files_to_delete:
            await stub.DeleteFiles(clouddrive_pb2.MultiFileRequest(path=files_to_delete), metadata=metadata)

        # 场景 C: 重新判定文件夹内容质量
        remaining = [f for f in sub_files if f.fullPathName not in files_to_delete and not f.isDirectory]
        max_size = max([f.size for f in remaining] or [0])

        if not remaining:
            # 清理后变为空，执行删除
            await stub.DeleteFiles(clouddrive_pb2.MultiFileRequest(path=[folder_path]), metadata=metadata)
            return f"🗑️ 清理了 {delete_count} 个垃圾文件，变为空目录已删除: `{folder_name}`"

        if max_size < SIZE_THRESHOLD_MB * 1024 * 1024:
            # 即使有文件，但如果都是几 MB 的小文件，也判定为无效任务
            await stub.DeleteFiles(clouddrive_pb2.MultiFileRequest(path=[folder_path]), metadata=metadata)
            return f"⚠️ 任务体积过小({max_size // (1024 * 1024)}MB)，已整体清理: `{folder_name}`"

        return f"🧹 已从 `{folder_name}` 中移除 {delete_count} 个垃圾文件。" if delete_count > 0 else None
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

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """全局错误拦截器，防止网络波动直接让程序崩溃"""
    logger.error(f"⚠️ 机器人运行时捕获到异常: {context.error}")


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """监听并处理发送的磁力链接、HTTP、电驴链接"""
    if update.effective_user.id not in ADMIN_IDS: return
    text = update.message.text.strip()

    if any(text.startswith(p) for p in ["magnet:", "http", "ed2k://"]):
        try:
            async with grpc.aio.insecure_channel(CD2_IP_PORT) as channel:
                stub = clouddrive_pb2_grpc.CloudDriveFileSrvStub(channel)
                metadata = [('authorization', f'Bearer {CD2_TOKEN}')]
                req = clouddrive_pb2.AddOfflineFileRequest(urls=text, toFolder=SAVE_PATH)
                res = await stub.AddOfflineFiles(req, metadata=metadata, timeout=15)
                if res.success:
                    await update.message.reply_text(
                        f"✅ 提交成功！\n📂 目录：`{SAVE_PATH}`\n提示：完成后发送 /clean 执行清理。")
                else:
                    await update.message.reply_text(f"❌ CD2 拒绝请求: {res.errorMessage}")
        except Exception as e:
            await update.message.reply_text(f"❌ 提交失败，CD2 连接异常: {str(e)}")


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
            async for reply in stub.GetSubFiles(root_req, metadata=metadata, timeout=30):
                if reply.subFiles:
                    for f in reply.subFiles:
                        if f.isDirectory:
                            res = await clean_task_folder(stub, metadata, f.fullPathName)
                            if res: results.append(res)

        report = "\n".join(results) if results else "✅ 下载目录非常整洁，无需清理。"
        await status_msg.edit_text(f"📊 **清理报告：**\n{report}", parse_mode='Markdown')
    except Exception as e:
        await status_msg.edit_text(f"❌ 无法执行清理: `{str(e)}`")


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
    app.run_polling()