# -*- coding: utf-8 -*-
"""
项目名称: CloudDrive2 Telegram 下载助手
功能描述:
1. 监听 Telegram 消息，自动提交 Magnet/HTTP/ed2k 链接至 CloudDrive2 离线下载。
2. 基于 Cron 表达式实现定时自动清理任务。
3. 智能过滤黑名单文件、清理空目录及低质量（小体积）文件夹。
作者: ymting
"""

import logging
import os
import grpc
import clouddrive_pb2
import clouddrive_pb2_grpc
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import Update, BotCommand
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters

# =================================================================
# 配置区：从环境变量加载参数 (支持 Docker 部署)
# =================================================================
# CloudDrive2 gRPC 服务地址 (例如 192.168.31.100:19798)
CD2_IP_PORT = os.getenv("CD2_ADDRESS", "127.0.0.1:19798")
# CloudDrive2 API Token
CD2_TOKEN = os.getenv("CD2_TOKEN", "")
# 离线下载在网盘内的保存路径 (例如 /115/离线下载)
SAVE_PATH = os.getenv("SAVE_PATH", "/115/离线下载")
# Telegram Bot Token (从 @BotFather 获取)
TG_BOT_TOKEN = os.getenv("TG_TOKEN", "")
# 管理员数字 ID 列表 (逗号分隔，例如 123456,789012)
ADMIN_IDS = [int(i) for i in os.getenv("ADMIN_IDS", "").split(",") if i.strip()]
# 网络代理地址 (可选，支持 http/socks5)
PROXY_URL = os.getenv("PROXY_URL", "")
# 自动化清理的 Cron 表达式 (默认每天凌晨 3:30 执行)
CLEAN_CRON = os.getenv("CLEAN_CRON", "30 3 * * *")
# 黑名单持久化文件路径
BLACKLIST_FILE = "blacklist.txt"
# 判定为“垃圾任务”的体积阈值 (单位: MB)
SIZE_THRESHOLD_MB = int(os.getenv("SIZE_THRESHOLD", "300"))

# 日志初始化
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# =================================================================
# 核心逻辑：文件清理与黑名单管理
# =================================================================

def get_blacklist():
    """获取黑名单关键词列表，若文件不存在则初始化默认值"""
    if not os.path.exists(BLACKLIST_FILE):
        default_list = ["广告", "promo", ".url", "txt", "readme", "扫码", "最新地址"]
        save_blacklist(default_list)
        return default_list
    with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def save_blacklist(keywords):
    """将关键词列表持久化保存到本地 txt 文件"""
    with open(BLACKLIST_FILE, "w", encoding="utf-8") as f:
        for k in keywords: f.write(f"{k}\n")


async def clean_task_folder(stub, metadata, folder_path):
    """
    清理单个文件夹的任务逻辑：
    1. 匹配并删除黑名单文件。
    2. 如果文件夹变为空或最大文件体积小于阈值，则整体删除。
    """
    try:
        req = clouddrive_pb2.ListSubFileRequest(path=folder_path)
        sub_files = []
        async for reply in stub.GetSubFiles(req, metadata=metadata, timeout=10):
            if reply.subFiles:
                sub_files.extend(reply.subFiles)

        # 处理空文件夹
        if not sub_files:
            await stub.DeleteFiles(clouddrive_pb2.MultiFileRequest(path=[folder_path]), metadata=metadata)
            return f"🗑️ 已删除空目录: {os.path.basename(folder_path)}"

        current_black = get_blacklist()
        # 筛选需要删除的黑名单文件
        files_to_delete = [f.fullPathName for f in sub_files if any(k.lower() in f.name.lower() for k in current_black)]

        if files_to_delete:
            await stub.DeleteFiles(clouddrive_pb2.MultiFileRequest(path=files_to_delete), metadata=metadata)

        # 统计非目录文件的最大体积
        max_file_size = max([f.size for f in sub_files if not f.isDirectory] or [0])

        # 判定是否为低质量垃圾任务
        if max_file_size < SIZE_THRESHOLD_MB * 1024 * 1024:
            await stub.DeleteFiles(clouddrive_pb2.MultiFileRequest(path=[folder_path]), metadata=metadata)
            return f"⚠️ 任务体积过小已清理: {os.path.basename(folder_path)}"

        return None
    except Exception as e:
        logger.error(f"清理文件夹 {folder_path} 时发生异常: {str(e)}")
        return None


# =================================================================
# 任务调度：定时清理与手动清理
# =================================================================

async def scheduled_clean():
    """执行自动化的目录扫描与清理"""
    logger.info("⏰ [Schedule] 启动定时自动清理任务...")
    async with grpc.aio.insecure_channel(CD2_IP_PORT) as channel:
        stub = clouddrive_pb2_grpc.CloudDriveFileSrvStub(channel)
        metadata = [('authorization', f'Bearer {CD2_TOKEN}')]

        # 扫描下载根目录
        root_req = clouddrive_pb2.ListSubFileRequest(path=SAVE_PATH)
        async for reply in stub.GetSubFiles(root_req, metadata=metadata, timeout=20):
            if reply.subFiles:
                for f in reply.subFiles:
                    # 仅处理子文件夹任务
                    if f.isDirectory:
                        await clean_task_folder(stub, metadata, f.fullPathName)
    logger.info("✅ [Schedule] 自动清理任务执行完毕。")


# =================================================================
# 交互处理：Telegram 命令与消息监听
# =================================================================

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """监听用户发送的下载链接 (支持 magnet, http, ed2k)"""
    if update.effective_user.id not in ADMIN_IDS: return

    text = update.message.text.strip()
    # 链接协议匹配
    if any(text.startswith(p) for p in ["magnet:?xt=", "http", "ed2k://"]):
        async with grpc.aio.insecure_channel(CD2_IP_PORT) as channel:
            stub = clouddrive_pb2_grpc.CloudDriveFileSrvStub(channel)
            metadata = [('authorization', f'Bearer {CD2_TOKEN}')]

            request = clouddrive_pb2.AddOfflineFileRequest(urls=text, toFolder=SAVE_PATH)
            response = await stub.AddOfflineFiles(request, metadata=metadata, timeout=10)

            if response.success:
                await update.message.reply_text(f"🚀 任务已成功提交至 CD2\n📂 路径: `{SAVE_PATH}`", parse_mode='Markdown')
            else:
                await update.message.reply_text(f"❌ 提交失败: {response.errorMessage}")


async def cmd_clean(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """响应 /clean 命令，手动触发清理"""
    if update.effective_user.id not in ADMIN_IDS: return
    msg = await update.message.reply_text("🔍 正在执行全量扫描清理...")
    await scheduled_clean()
    await msg.edit_text("📊 清理操作已完成！具体变动请查看容器日志。")


async def cmd_blacklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """响应 /blacklist 命令，管理或查看黑名单关键词"""
    if update.effective_user.id not in ADMIN_IDS: return
    current = get_blacklist()

    if context.args:
        new_word = " ".join(context.args)
        if new_word not in current:
            current.append(new_word)
            save_blacklist(current)
            await update.message.reply_text(f"➕ 已添加黑名单关键词: `{new_word}`", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"📝 当前黑名单关键词:\n`{', '.join(current)}`", parse_mode='Markdown')


async def post_init(application):
    """Bot 启动后的初始化操作，注册命令菜单"""
    await application.bot.set_my_commands([
        BotCommand("clean", "立即执行手动清理"),
        BotCommand("blacklist", "查看或添加黑名单关键词")
    ])


# =================================================================
# 程序入口
# =================================================================

if __name__ == '__main__':
    # 1. 构造机器人 Application
    builder = ApplicationBuilder().token(TG_BOT_TOKEN).post_init(post_init)

    # 应用网络代理 (如果配置)
    if PROXY_URL:
        logger.info(f"检测到代理配置: {PROXY_URL}")
        builder.proxy(PROXY_URL)
        builder.get_updates_proxy(PROXY_URL)

    app = builder.build()

    # 2. 初始化 Cron 定时调度器
    scheduler = AsyncIOScheduler()
    try:
        # 使用 CronTrigger 实现精准定时
        scheduler.add_job(scheduled_clean, CronTrigger.from_crontab(CLEAN_CRON))
        scheduler.start()
        logger.info(f"📅 定时任务已启用，Cron 表达式: [{CLEAN_CRON}]")
    except Exception as e:
        logger.error(f"❌ Cron 表达式解析失败: {str(e)}")

    # 3. 注册消息处理器
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_link))
    app.add_handler(CommandHandler("clean", cmd_clean))
    app.add_handler(CommandHandler("blacklist", cmd_blacklist))
    app.add_handler(CommandHandler("add_black", cmd_blacklist))

    # 4. 启动机器人
    logger.info("🚀 CloudDrive2 Bot 已就绪，开始轮询消息...")
    app.run_polling()