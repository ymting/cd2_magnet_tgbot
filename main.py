# -*- coding: utf-8 -*-
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

# ==========================================
# 配置区 (建议通过环境变量注入)
# ==========================================
CD2_IP_PORT = os.getenv("CD2_ADDRESS", "127.0.0.1:19798")
CD2_TOKEN = os.getenv("CD2_TOKEN", "")
SAVE_PATH = os.getenv("SAVE_PATH", "/115/离线下载")
TG_BOT_TOKEN = os.getenv("TG_TOKEN", "")
ADMIN_IDS = [int(i) for i in os.getenv("ADMIN_IDS", "").split(",") if i.strip()]
PROXY_URL = os.getenv("PROXY_URL", "")
CLEAN_CRON = os.getenv("CLEAN_CRON", "30 3 * * *")  # 默认凌晨 3:30 执行
BLACKLIST_FILE = "blacklist.txt"
SIZE_THRESHOLD_MB = int(os.getenv("SIZE_THRESHOLD", "300"))

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# ==========================================
# 核心逻辑：文件清理引擎
# ==========================================

def get_blacklist():
    """获取黑名单，若文件不存在则初始化"""
    if not os.path.exists(BLACKLIST_FILE):
        default_list = ["广告", "promo", ".url", "txt", "readme", "扫码"]
        with open(BLACKLIST_FILE, "w", encoding="utf-8") as f:
            for k in default_list: f.write(f"{k}\n")
        return default_list
    with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


async def clean_task_folder(stub, metadata, folder_path):
    """
    清理逻辑并返回详细的操作描述。
    若失败，返回以 '❌' 开头的错误提示。
    """
    folder_name = os.path.basename(folder_path)
    try:
        req = clouddrive_pb2.ListSubFileRequest(path=folder_path)
        sub_files = []
        async for reply in stub.GetSubFiles(req, metadata=metadata, timeout=10):
            if reply.subFiles: sub_files.extend(reply.subFiles)

        if not sub_files:
            await stub.DeleteFiles(clouddrive_pb2.MultiFileRequest(path=[folder_path]), metadata=metadata)
            return f"🗑️ 发现空目录已删除: `{folder_name}`"

        current_black = get_blacklist()
        files_to_delete = [f.fullPathName for f in sub_files if any(k.lower() in f.name.lower() for k in current_black)]
        delete_count = len(files_to_delete)

        if files_to_delete:
            await stub.DeleteFiles(clouddrive_pb2.MultiFileRequest(path=files_to_delete), metadata=metadata)

        # 重新判定文件夹质量
        remaining = [f for f in sub_files if f.fullPathName not in files_to_delete and not f.isDirectory]
        max_size = max([f.size for f in remaining] or [0])

        if not remaining:
            await stub.DeleteFiles(clouddrive_pb2.MultiFileRequest(path=[folder_path]), metadata=metadata)
            return f"🗑️ 清理了 {delete_count} 个垃圾文件，变为空目录已删除: `{folder_name}`"

        if max_size < SIZE_THRESHOLD_MB * 1024 * 1024:
            await stub.DeleteFiles(clouddrive_pb2.MultiFileRequest(path=[folder_path]), metadata=metadata)
            return f"⚠️ 最大文件过小，已整体清理: `{folder_name}`"

        return f"🧹 已从 `{folder_name}` 中移除 {delete_count} 个垃圾文件。" if delete_count > 0 else None

    except Exception as e:
        # 清理失败时的提示信息
        logger.error(f"清理 {folder_name} 失败: {str(e)}")
        return f"❌ 处理 `{folder_name}` 异常: {str(e)}"


# ==========================================
# 任务触发与交互
# ==========================================

async def run_auto_clean():
    """定时任务调用的清理函数"""
    logger.info("⏰ 执行定时自动清理...")
    async with grpc.aio.insecure_channel(CD2_IP_PORT) as channel:
        stub = clouddrive_pb2_grpc.CloudDriveFileSrvStub(channel)
        metadata = [('authorization', f'Bearer {CD2_TOKEN}')]
        root_req = clouddrive_pb2.ListSubFileRequest(path=SAVE_PATH)
        async for reply in stub.GetSubFiles(root_req, metadata=metadata):
            if reply.subFiles:
                for f in reply.subFiles:
                    if f.isDirectory: await clean_task_folder(stub, metadata, f.fullPathName)


async def cmd_clean(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """手动清理命令处理器"""
    if update.effective_user.id not in ADMIN_IDS: return
    status_msg = await update.message.reply_text("🔍 正在扫描下载目录，请稍候...")

    results = []
    try:
        async with grpc.aio.insecure_channel(CD2_IP_PORT) as channel:
            stub = clouddrive_pb2_grpc.CloudDriveFileSrvStub(channel)
            metadata = [('authorization', f'Bearer {CD2_TOKEN}')]
            root_req = clouddrive_pb2.ListSubFileRequest(path=SAVE_PATH)

            async for reply in stub.GetSubFiles(root_req, metadata=metadata, timeout=15):
                if reply.subFiles:
                    for f in reply.subFiles:
                        if f.isDirectory:
                            res = await clean_task_folder(stub, metadata, f.fullPathName)
                            if res: results.append(res)

        if not results:
            await status_msg.edit_text("✅ 扫描完成，没有需要清理的内容。")
        else:
            report = "\n".join(results)
            await status_msg.edit_text(f"📊 **清理报告：**\n{report}", parse_mode='Markdown')

    except Exception as e:
        await status_msg.edit_text(f"❌ 严重错误：无法连接到 CD2 服务\n详情：`{str(e)}`", parse_mode='Markdown')


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理提交链接"""
    if update.effective_user.id not in ADMIN_IDS: return
    text = update.message.text.strip()
    if any(text.startswith(p) for p in ["magnet:", "http", "ed2k://"]):
        try:
            async with grpc.aio.insecure_channel(CD2_IP_PORT) as channel:
                stub = clouddrive_pb2_grpc.CloudDriveFileSrvStub(channel)
                metadata = [('authorization', f'Bearer {CD2_TOKEN}')]
                req = clouddrive_pb2.AddOfflineFileRequest(urls=text, toFolder=SAVE_PATH)
                res = await stub.AddOfflineFiles(req, metadata=metadata)
                if res.success:
                    await update.message.reply_text(
                        f"✅ 提交成功！\n📂 目录：`{SAVE_PATH}`\n提示：下载完成后发送 /clean 执行清理。")
                else:
                    await update.message.reply_text(f"❌ CD2 拒绝请求: {res.errorMessage}")
        except Exception as e:
            await update.message.reply_text(f"❌ 提交失败，网络连接异常: {str(e)}")


async def post_init(application):
    await application.bot.set_my_commands(
        [BotCommand("clean", "立即执行全量扫描清理"), BotCommand("blacklist", "查看或添加黑名单")])


if __name__ == '__main__':
    builder = ApplicationBuilder().token(TG_BOT_TOKEN).post_init(post_init)
    if PROXY_URL:
        builder.proxy(PROXY_URL)
        builder.get_updates_proxy(PROXY_URL)

    app = builder.build()

    # 开启 Cron 定时调度
    scheduler = AsyncIOScheduler()
    scheduler.add_job(run_auto_clean, CronTrigger.from_crontab(CLEAN_CRON))
    scheduler.start()

    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_link))
    app.add_handler(CommandHandler("clean", cmd_clean))

    logger.info("🚀 机器人启动，监听指令中...")
    app.run_polling()