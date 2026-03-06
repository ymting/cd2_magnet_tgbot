# -*- coding: utf-8 -*-
"""
CloudDrive2 Telegram 助手
功能：接收磁力链接提交离线下载，并提供基于文件大小和黑名单的自动清理功能。
"""

import logging
import os
import grpc
import clouddrive_pb2
import clouddrive_pb2_grpc
from telegram import Update, BotCommand
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters

# ==========================================
# 环境变量配置 (从 Docker 容器或系统环境获取)
# ==========================================
CD2_IP_PORT = os.getenv("CD2_ADDRESS", "127.0.0.1:19798")
CD2_TOKEN = os.getenv("CD2_TOKEN", "")
TG_BOT_TOKEN = os.getenv("TG_TOKEN", "")
SAVE_PATH = os.getenv("SAVE_PATH", "/115/离线下载")
# 管理员ID，只有列表中的用户能操作机器人。多个ID请用逗号分隔
ADMIN_IDS = [int(i) for i in os.getenv("ADMIN_IDS", "").split(",") if i.strip()]
BLACKLIST_FILE = "blacklist.txt"
SIZE_THRESHOLD_MB = int(os.getenv("SIZE_THRESHOLD", "300"))

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ==========================================
# 黑名单持久化管理 (文本文件操作)
# ==========================================
def get_blacklist():
    """读取黑名单关键词"""
    if not os.path.exists(BLACKLIST_FILE):
        # 默认内置一些常见的垃圾文件关键词
        default_list = ["广告", "promo", ".url", "txt", "readme", "扫码", "最新地址"]
        save_blacklist(default_list)
        return default_list
    with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def save_blacklist(keywords):
    """保存黑名单关键词"""
    with open(BLACKLIST_FILE, "w", encoding="utf-8") as f:
        for k in keywords:
            f.write(f"{k}\n")


# ==========================================
# CloudDrive2 文件清理核心逻辑
# ==========================================
async def clean_task_folder(stub, metadata, folder_path):
    """
    清理逻辑：
    1. 删除文件名命中黑名单的文件。
    2. 如果文件夹清理后变为空，或文件夹内没有任何文件达到 300MB，则删除整个文件夹。
    """
    norm_path = folder_path.rstrip('/')
    norm_root = SAVE_PATH.rstrip('/')

    # 安全检查：禁止操作根目录或非下载目录
    if not norm_path.startswith(norm_root) or norm_path == norm_root:
        return None

    try:
        # 使用 gRPC 流式接口获取文件列表
        req = clouddrive_pb2.ListSubFileRequest(path=folder_path)
        sub_files = []
        async for reply in stub.GetSubFiles(req, metadata=metadata, timeout=10):
            if reply.subFiles:
                sub_files.extend(reply.subFiles)

        if not sub_files:
            # 发现纯空文件夹
            await stub.DeleteFiles(clouddrive_pb2.MultiFileRequest(path=[folder_path]), metadata=metadata)
            return f"🗑️ 发现空目录已删除: `{os.path.basename(folder_path)}`"

        current_black = get_blacklist()
        files_to_delete = []
        max_file_size = 0
        valid_files_count = 0

        for f in sub_files:
            # A. 检查黑名单
            if any(k.lower() in f.name.lower() for k in current_black):
                files_to_delete.append(f.fullPathName)  # 使用 fullPathName
                continue

            # B. 统计非垃圾文件信息
            if not f.isDirectory:
                valid_files_count += 1
                if f.size > max_file_size:
                    max_file_size = f.size

        # 执行黑名单文件删除
        if files_to_delete:
            await stub.DeleteFiles(clouddrive_pb2.MultiFileRequest(path=files_to_delete), metadata=metadata)

        # C. 判定最终存留
        threshold_bytes = SIZE_THRESHOLD_MB * 1024 * 1024

        if valid_files_count == 0:
            # 删完黑名单后没有文件了
            await stub.DeleteFiles(clouddrive_pb2.MultiFileRequest(path=[folder_path]), metadata=metadata)
            return f"🗑️ 清理后变为空目录，已删除: `{os.path.basename(folder_path)}`"

        if max_file_size < threshold_bytes:
            # 虽然有文件，但都不够 300MB
            await stub.DeleteFiles(clouddrive_pb2.MultiFileRequest(path=[folder_path]), metadata=metadata)
            return f"⚠️ 最大文件 < {SIZE_THRESHOLD_MB}MB，已视为垃圾任务删除: `{os.path.basename(folder_path)}`"

        return f"✅ `{os.path.basename(folder_path)}` 检查通过。"

    except grpc.RpcError as e:
        return f"❌ 处理 `{os.path.basename(folder_path)}` 失败 | 错误: {e.code()}"
    except Exception as e:
        return f"❌ 处理 `{os.path.basename(folder_path)}` 发生未知异常: {str(e)}"


# ==========================================
# Telegram 机器人命令响应逻辑
# ==========================================
async def handle_magnet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理磁力链接"""
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
                        f"✅ 提交成功！\n📂 路径：`{SAVE_PATH}`\n提示：下载完成后发送 /clean 执行清理。")
                else:
                    await status_msg.edit_text(f"❌ CD2 拒绝请求: {response.errorMessage}")
        except Exception as e:
            await status_msg.edit_text(f"⚠️ 提交失败: {str(e)}")


async def cmd_clean(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """扫描并执行清理命令"""
    if update.effective_user.id not in ADMIN_IDS: return
    msg = await update.message.reply_text("🔍 正在扫描下载目录下的子文件夹...")

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
                await msg.edit_text("📁 下载目录中没有发现子文件夹。")
                return

            results = []
            for folder in task_folders:
                res = await clean_task_folder(stub, metadata, folder)
                if res: results.append(res)

            report = "\n".join(results) if results else "扫描完成，没有发现需删除的文件夹。"
            if len(report) > 4000: report = report[:3900] + "\n...(由于内容过多已截断)"
            await msg.edit_text(f"📊 **清理报告：**\n{report}", parse_mode='Markdown')
    except Exception as e:
        await msg.edit_text(f"⚠️ 连接 CD2 失败: {str(e)}")


async def cmd_black(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看或添加黑名单关键词"""
    if update.effective_user.id not in ADMIN_IDS: return
    current = get_blacklist()
    if context.args:
        new_key = " ".join(context.args)
        if new_key not in current:
            current.append(new_key);
            save_blacklist(current)
            await update.message.reply_text(f"➕ 已添加黑名单关键词: `{new_key}`", parse_mode='Markdown')
    else:
        black_str = ", ".join(current)
        await update.message.reply_text(f"📝 当前黑名单关键词：\n`{black_str}`\n\n添加：`/add_black 关键词`",
                                        parse_mode='Markdown')


async def post_init(application):
    """
    程序初始化：自动在 Telegram 中注册 "/" 菜单命令提示。
    """
    await application.bot.set_my_commands([
        BotCommand("clean", "清理下载目录下的垃圾文件夹"),
        BotCommand("blacklist", "查看或添加黑名单关键词")
    ])


if __name__ == '__main__':
    # 启动应用，并绑定 post_init 用于自动同步菜单
    app = ApplicationBuilder().token(TG_BOT_TOKEN).post_init(post_init).build()

    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_magnet))
    app.add_handler(CommandHandler("clean", cmd_clean))
    app.add_handler(CommandHandler("blacklist", cmd_black))
    app.add_handler(CommandHandler("add_black", cmd_black))

    print("🚀 机器人启动成功，正在监听消息...")
    app.run_polling()