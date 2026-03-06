# -*- coding: utf-8 -*-
import logging
import os
import grpc
import clouddrive_pb2
import clouddrive_pb2_grpc
from telegram import Update, BotCommand
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters

# ==========================================
# 配置区 (优先从环境变量读取)
# ==========================================
# 1. CloudDrive2 相关配置
CD2_IP_PORT = os.getenv("CD2_ADDRESS", "127.0.0.1:19798")
CD2_TOKEN = os.getenv("CD2_TOKEN", "")
SAVE_PATH = os.getenv("SAVE_PATH", "/115/离线下载")

# 2. Telegram 机器人相关配置
TG_BOT_TOKEN = os.getenv("TG_TOKEN", "")
# 管理员ID，支持逗号分隔，例如: 1234567,8901234
ADMIN_IDS = [int(i) for i in os.getenv("ADMIN_IDS", "").split(",") if i.strip()]
# 代理配置：支持 http://ip:port 或 socks5://ip:port
PROXY_URL = os.getenv("PROXY_URL", "")

# 3. 清理逻辑相关配置
BLACKLIST_FILE = "blacklist.txt"
SIZE_THRESHOLD_MB = int(os.getenv("SIZE_THRESHOLD", "300"))

# 日志配置
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# ==========================================
# 核心功能逻辑
# ==========================================

def get_blacklist():
    """读取黑名单关键词文件"""
    if not os.path.exists(BLACKLIST_FILE):
        default_list = ["广告", "promo", ".url", "txt", "readme", "扫码", "最新地址"]
        with open(BLACKLIST_FILE, "w", encoding="utf-8") as f:
            for k in default_list: f.write(f"{k}\n")
        return default_list
    with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def save_blacklist(keywords):
    """保存黑名单关键词"""
    with open(BLACKLIST_FILE, "w", encoding="utf-8") as f:
        for k in keywords: f.write(f"{k}\n")


async def clean_task_folder(stub, metadata, folder_path):
    """清理子文件夹内的黑名单文件及判定体积"""
    norm_path = folder_path.rstrip('/')
    norm_root = SAVE_PATH.rstrip('/')

    if not norm_path.startswith(norm_root) or norm_path == norm_root:
        return None

    try:
        # 获取文件列表
        req = clouddrive_pb2.ListSubFileRequest(path=folder_path)
        sub_files = []
        async for reply in stub.GetSubFiles(req, metadata=metadata, timeout=10):
            if reply.subFiles:
                sub_files.extend(reply.subFiles)

        if not sub_files:
            await stub.DeleteFiles(clouddrive_pb2.MultiFileRequest(path=[folder_path]), metadata=metadata)
            return f"🗑️ 发现空目录已删除: `{os.path.basename(folder_path)}`"

        current_black = get_blacklist()
        files_to_delete = []
        max_file_size = 0
        valid_files_count = 0

        for f in sub_files:
            if any(k.lower() in f.name.lower() for k in current_black):
                files_to_delete.append(f.fullPathName)
                continue
            if not f.isDirectory:
                valid_files_count += 1
                if f.size > max_file_size:
                    max_file_size = f.size

        # 执行黑名单删除
        if files_to_delete:
            await stub.DeleteFiles(clouddrive_pb2.MultiFileRequest(path=files_to_delete), metadata=metadata)

        # 判定文件夹是否保留
        threshold_bytes = SIZE_THRESHOLD_MB * 1024 * 1024
        if valid_files_count == 0:
            await stub.DeleteFiles(clouddrive_pb2.MultiFileRequest(path=[folder_path]), metadata=metadata)
            return f"🗑️ 清理后变为空目录，已删除: `{os.path.basename(folder_path)}`"

        if max_file_size < threshold_bytes:
            await stub.DeleteFiles(clouddrive_pb2.MultiFileRequest(path=[folder_path]), metadata=metadata)
            return f"⚠️ 最大文件 < {SIZE_THRESHOLD_MB}MB，已视为垃圾任务删除: `{os.path.basename(folder_path)}`"

        return f"✅ `{os.path.basename(folder_path)}` 检查通过。"

    except grpc.RpcError as e:
        return f"❌ 处理 `{os.path.basename(folder_path)}` 失败: {e.details()}"
    except Exception as e:
        return f"❌ 处理 `{os.path.basename(folder_path)}` 异常: {str(e)}"


# ==========================================
# Telegram 指令处理
# ==========================================

async def handle_magnet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    text = update.message.text.strip()
    if text.startswith("magnet:?xt=") or text.startswith("http"):
        status_msg = await update.message.reply_text("⏳ 正在提交离线下载任务...")
        try:
            async with grpc.aio.insecure_channel(CD2_IP_PORT) as channel:
                stub = clouddrive_pb2_grpc.CloudDriveFileSrvStub(channel)
                metadata = [('authorization', f'Bearer {CD2_TOKEN}')]
                request = clouddrive_pb2.AddOfflineFileRequest(urls=text, toFolder=SAVE_PATH)
                response = await stub.AddOfflineFiles(request, metadata=metadata, timeout=10)
                if response.success:
                    await status_msg.edit_text(
                        f"✅ 提交成功！\n📂 目录：`{SAVE_PATH}`\n提示：下载完成后发送 /clean 执行清理。")
                else:
                    await status_msg.edit_text(f"❌ 失败：{response.errorMessage}")
        except Exception as e:
            await status_msg.edit_text(f"⚠️ 报错: {str(e)}")


async def cmd_clean(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    msg = await update.message.reply_text("🔍 正在扫描下载目录...")
    try:
        async with grpc.aio.insecure_channel(CD2_IP_PORT) as channel:
            stub = clouddrive_pb2_grpc.CloudDriveFileSrvStub(channel)
            metadata = [('authorization', f'Bearer {CD2_TOKEN}')]
            root_req = clouddrive_pb2.ListSubFileRequest(path=SAVE_PATH)
            task_folders = []
            async for reply in stub.GetSubFiles(root_req, metadata=metadata, timeout=10):
                if reply.subFiles:
                    for f in reply.subFiles:
                        if f.isDirectory: task_folders.append(f.fullPathName)
            if not task_folders:
                await msg.edit_text("📁 暂无子文件夹，无需清理。")
                return
            results = [await clean_task_folder(stub, metadata, folder) for folder in task_folders]
            report = "\n".join([r for r in results if r])
            await msg.edit_text(f"📊 **清理报告：**\n{report or '无变化'}", parse_mode='Markdown')
    except Exception as e:
        await msg.edit_text(f"⚠️ 清理失败: {str(e)}")


async def cmd_black(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    current = get_blacklist()
    if context.args:
        new_key = " ".join(context.args)
        if new_key not in current:
            current.append(new_key);
            save_blacklist(current)
            await update.message.reply_text(f"➕ 已添加: `{new_key}`", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"📝 当前黑名单关键词：\n`{', '.join(current)}`", parse_mode='Markdown')


async def post_init(application):
    """自动注册菜单命令"""
    await application.bot.set_my_commands([
        BotCommand("clean", "清理下载目录下的垃圾文件夹"),
        BotCommand("blacklist", "查看或添加黑名单关键词")
    ])


if __name__ == '__main__':
    # 代理逻辑
    builder = ApplicationBuilder().token(TG_BOT_TOKEN).post_init(post_init)
    if PROXY_URL:
        logger.info(f"启用网络代理: {PROXY_URL}")
        builder.proxy(PROXY_URL)
        builder.get_updates_proxy(PROXY_URL)

    app = builder.build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_magnet))
    app.add_handler(CommandHandler("clean", cmd_clean))
    app.add_handler(CommandHandler("blacklist", cmd_black))
    app.add_handler(CommandHandler("add_black", cmd_black))

    logger.info("🚀 机器人启动成功")
    app.run_polling()