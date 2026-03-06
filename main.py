# -*- coding: utf-8 -*-
"""
项目名称: CloudDrive2 Telegram 离线下载管家
功能描述:
    1. 自动化提交: 监听 TG 消息并提交磁力、HTTP、ed2k 链接至 CD2。
    2. 智能清理: 定时或手动扫描下载目录，自动剔除广告、垃圾文件及无效任务。
    3. Cron 调度: 使用标准的 Cron 表达式精准控制后台任务执行时间。
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

# ==========================================
# 1. 配置加载 (优先读取环境变量)
# ==========================================
CD2_IP_PORT = os.getenv("CD2_ADDRESS", "127.0.0.1:19798")  # CD2 的 gRPC 访问地址
CD2_TOKEN = os.getenv("CD2_TOKEN", "")  # CD2 的 API 访问令牌
SAVE_PATH = os.getenv("SAVE_PATH", "/115/离线下载")  # 离线下载默认保存路径
TG_BOT_TOKEN = os.getenv("TG_TOKEN", "")  # Telegram 机器人 Token
ADMIN_IDS = [int(i) for i in os.getenv("ADMIN_IDS", "").split(",") if i.strip()]  # 管理员 ID 列表
PROXY_URL = os.getenv("PROXY_URL", "")  # 网络代理 (可选)
CLEAN_CRON = os.getenv("CLEAN_CRON", "30 3 * * *")  # 自动清理时间 (默认凌晨 3:30)
BLACKLIST_FILE = "blacklist.txt"  # 黑名单本地存储文件
SIZE_THRESHOLD_MB = int(os.getenv("SIZE_THRESHOLD", "300"))  # 有效文件体积阈值 (MB)

# 配置日志输出格式
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# ==========================================
# 2. 核心逻辑函数
# ==========================================

def get_blacklist():
    """读取黑名单列表，若文件不存在则初始化默认关键词"""
    if not os.path.exists(BLACKLIST_FILE):
        default_list = ["广告", "promo", ".url", "txt", "readme", "扫码", "最新地址"]
        with open(BLACKLIST_FILE, "w", encoding="utf-8") as f:
            for k in default_list: f.write(f"{k}\n")
        return default_list
    with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


async def clean_task_folder(stub, metadata, folder_path):
    """
    深度清理逻辑：
    - 删除匹配黑名单的文件
    - 如果清理后为空或文件夹内无足够大的文件，则删除整个目录
    """
    folder_name = os.path.basename(folder_path)
    try:
        # 1. 获取文件夹内所有文件列表
        req = clouddrive_pb2.ListSubFileRequest(path=folder_path)
        sub_files = []
        async for reply in stub.GetSubFiles(req, metadata=metadata, timeout=10):
            if reply.subFiles: sub_files.extend(reply.subFiles)

        # 2. 如果是空目录，直接删除
        if not sub_files:
            await stub.DeleteFiles(clouddrive_pb2.MultiFileRequest(path=[folder_path]), metadata=metadata)
            return f"🗑️ 发现空目录已删除: `{folder_name}`"

        # 3. 匹配黑名单关键词进行文件级删除
        current_black = get_blacklist()
        files_to_delete = [f.fullPathName for f in sub_files if any(k.lower() in f.name.lower() for k in current_black)]
        delete_count = len(files_to_delete)

        if files_to_delete:
            await stub.DeleteFiles(clouddrive_pb2.MultiFileRequest(path=files_to_delete), metadata=metadata)

        # 4. 判断剩余内容是否符合保留要求
        remaining = [f for f in sub_files if f.fullPathName not in files_to_delete and not f.isDirectory]
        max_size = max([f.size for f in remaining] or [0])

        # 如果没有剩余文件，或者文件都太小（判定为垃圾任务）
        if not remaining:
            await stub.DeleteFiles(clouddrive_pb2.MultiFileRequest(path=[folder_path]), metadata=metadata)
            return f"🗑️ 清理了 {delete_count} 个垃圾文件，变为空目录已删除: `{folder_name}`"

        if max_size < SIZE_THRESHOLD_MB * 1024 * 1024:
            await stub.DeleteFiles(clouddrive_pb2.MultiFileRequest(path=[folder_path]), metadata=metadata)
            return f"⚠️ 任务体积过小({max_size // (1024 * 1024)}MB)，已整体清理: `{folder_name}`"

        return f"🧹 已从 `{folder_name}` 中移除 {delete_count} 个垃圾文件。" if delete_count > 0 else None
    except Exception as e:
        return f"❌ 处理 `{folder_name}` 异常: {str(e)}"


async def run_auto_clean():
    """定时任务触发器：扫描根目录并执行清理"""
    logger.info("⏰ [Schedule] 开始执行自动清理任务...")
    async with grpc.aio.insecure_channel(CD2_IP_PORT) as channel:
        stub = clouddrive_pb2_grpc.CloudDriveFileSrvStub(channel)
        metadata = [('authorization', f'Bearer {CD2_TOKEN}')]
        root_req = clouddrive_pb2.ListSubFileRequest(path=SAVE_PATH)
        async for reply in stub.GetSubFiles(root_req, metadata=metadata):
            if reply.subFiles:
                for f in reply.subFiles:
                    if f.isDirectory: await clean_task_folder(stub, metadata, f.fullPathName)


# ==========================================
# 3. Telegram 指令处理
# ==========================================

async def cmd_clean(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """手动执行清理命令 (/clean)"""
    if update.effective_user.id not in ADMIN_IDS: return
    status_msg = await update.message.reply_text("🔍 正在扫描下载目录，请稍后...")
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

        report = "\n".join(results) if results else "✅ 目录非常整洁，无需清理。"
        await status_msg.edit_text(f"📊 **清理报告：**\n{report}", parse_mode='Markdown')
    except Exception as e:
        await status_msg.edit_text(f"❌ 连接 CD2 失败: `{str(e)}`")


async def cmd_blacklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """黑名单管理命令 (/blacklist)"""
    if update.effective_user.id not in ADMIN_IDS: return
    current = get_blacklist()
    # 如果命令带参数则添加黑名单，如: /blacklist 广告词
    if context.args:
        new_word = " ".join(context.args)
        if new_word not in current:
            current.append(new_word)
            with open(BLACKLIST_FILE, "w", encoding="utf-8") as f:
                for k in current: f.write(f"{k}\n")
            await update.message.reply_text(f"➕ 已添加黑名单关键词: `{new_word}`", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"📝 当前黑名单:\n`{', '.join(current)}`", parse_mode='Markdown')


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理用户发送的下载链接 (Magnet/HTTP/ed2k)"""
    if update.effective_user.id not in ADMIN_IDS: return
    text = update.message.text.strip()
    # 协议白名单匹配
    if any(text.startswith(p) for p in ["magnet:", "http", "ed2k://"]):
        try:
            async with grpc.aio.insecure_channel(CD2_IP_PORT) as channel:
                stub = clouddrive_pb2_grpc.CloudDriveFileSrvStub(channel)
                metadata = [('authorization', f'Bearer {CD2_TOKEN}')]
                req = clouddrive_pb2.AddOfflineFileRequest(urls=text, toFolder=SAVE_PATH)
                res = await stub.AddOfflineFiles(req, metadata=metadata)
                if res.success:
                    await update.message.reply_text(
                        f"✅ 提交成功！\n📂 目录：`{SAVE_PATH}`\n提示：完成后发送 /clean 手动清理。")
                else:
                    await update.message.reply_text(f"❌ 提交失败: {res.errorMessage}")
        except Exception as e:
            await update.message.reply_text(f"❌ 提交异常，请检查 CD2 连接: {str(e)}")


# ==========================================
# 4. 初始化与启动
# ==========================================

async def post_init(application):
    """机器人启动后的初始化：设置菜单、开启定时任务"""
    # 注册 TG 客户端按钮菜单
    await application.bot.set_my_commands([
        BotCommand("clean", "手动扫描并执行清理"),
        BotCommand("blacklist", "查看或添加黑名单")
    ])
    # 初始化异步调度器
    scheduler = AsyncIOScheduler()
    scheduler.add_job(run_auto_clean, CronTrigger.from_crontab(CLEAN_CRON))
    scheduler.start()
    logger.info(f"📅 定时任务系统已就绪，当前 Cron: [{CLEAN_CRON}]")


if __name__ == '__main__':
    # 构造机器人应用
    builder = ApplicationBuilder().token(TG_BOT_TOKEN).post_init(post_init)

    # 设置网络代理
    if PROXY_URL:
        logger.info(f"正在启用代理: {PROXY_URL}")
        builder.proxy(PROXY_URL)
        builder.get_updates_proxy(PROXY_URL)

    app = builder.build()

    # 注册各类处理器
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_link))
    app.add_handler(CommandHandler("clean", cmd_clean))
    app.add_handler(CommandHandler("blacklist", cmd_blacklist))

    logger.info("🚀 CD2 Bot 启动成功，正在监听消息...")
    app.run_polling()