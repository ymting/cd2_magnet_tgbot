# -*- coding: utf-8 -*-
"""
项目名称：CloudDrive2 Telegram 助手 (支持磁力/HTTP/ed2k)
功能描述：
1. 监听 Telegram 消息，自动提交下载链接至 CloudDrive2。
2. 自动清理下载任务中的广告文件、垃圾文件及空文件夹。
3. 自动删除不含大文件（小于设定阈值）的“伪资源”文件夹。
"""

import logging
import os
import grpc
import clouddrive_pb2
import clouddrive_pb2_grpc
from telegram import Update, BotCommand
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters

# =================================================================
# 配置区：以下参数优先从 Docker 环境变量读取，若无则使用默认值
# =================================================================

# [CloudDrive2 地址] 格式为 "IP:端口"，例如 "192.168.31.224:19798"
CD2_IP_PORT = os.getenv("CD2_ADDRESS", "127.0.0.1:19798")

# [CloudDrive2 Token] 在 CD2 设置中获取的 API 访问令牌
CD2_TOKEN = os.getenv("CD2_TOKEN", "")

# [下载保存路径] 必须是 CD2 内部挂载的路径，例如 "/115/离线下载"
SAVE_PATH = os.getenv("SAVE_PATH", "/115/离线下载")

# [Telegram Bot Token] 从 @BotFather 获取的机器人密钥
TG_BOT_TOKEN = os.getenv("TG_TOKEN", "")

# [管理员 ID] 允许使用机器人的数字 ID。多个 ID 请用英文逗号分隔，例如 "123456,789012"
# 程序会自动将其转换为数字列表进行匹配
ADMIN_IDS = [int(i) for i in os.getenv("ADMIN_IDS", "").split(",") if i.strip()]

# [网络代理] 若服务器在中国大陆，需配置代理以连接 Telegram，例如 "http://192.168.31.10:7890"
# 支持 http:// 或 socks5:// 协议
PROXY_URL = os.getenv("PROXY_URL", "")

# [黑名单文件名] 存储在程序同级目录下，用于持久化记录过滤关键词
BLACKLIST_FILE = "blacklist.txt"

# [文件体积阈值] 判定为“有效任务”的最小文件体积（单位：MB）。默认 300MB
SIZE_THRESHOLD_MB = int(os.getenv("SIZE_THRESHOLD", "300"))

# =================================================================

# 日志配置
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# --- 黑名单管理逻辑 ---
def get_blacklist():
    """从本地文件读取黑名单关键词，若文件不存在则创建默认列表"""
    if not os.path.exists(BLACKLIST_FILE):
        default_list = ["广告", "promo", ".url", "txt", "readme", "扫码", "最新地址"]
        save_blacklist(default_list)
        return default_list
    with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def save_blacklist(keywords):
    """将关键词列表写入本地文件"""
    with open(BLACKLIST_FILE, "w", encoding="utf-8") as f:
        for k in keywords: f.write(f"{k}\n")


# --- CloudDrive2 核心清理函数 ---
async def clean_task_folder(stub, metadata, folder_path):
    """
    深度清理逻辑：
    1. 遍历子文件夹，删除匹配黑名单的文件。
    2. 计算剩余文件中最大的文件体积。
    3. 若文件夹变为空，或最大文件小于 SIZE_THRESHOLD_MB，则删除该文件夹。
    """
    norm_path = folder_path.rstrip('/')
    norm_root = SAVE_PATH.rstrip('/')

    # 路径安全检查，防止误删根目录
    if not norm_path.startswith(norm_root) or norm_path == norm_root:
        return None

    try:
        # 获取文件夹下的文件列表
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
            # 1. 匹配黑名单关键词
            if any(k.lower() in f.name.lower() for k in current_black):
                files_to_delete.append(f.fullPathName)  # 此处字段必须对应 pb2 定义的 fullPathName
                continue

            # 2. 统计有效文件及其最大体积
            if not f.isDirectory:
                valid_files_count += 1
                if f.size > max_file_size:
                    max_file_size = f.size

        # 执行黑名单文件删除
        if files_to_delete:
            await stub.DeleteFiles(clouddrive_pb2.MultiFileRequest(path=files_to_delete), metadata=metadata)

        # 3. 判定文件夹是否存留
        threshold_bytes = SIZE_THRESHOLD_MB * 1024 * 1024
        if valid_files_count == 0:
            await stub.DeleteFiles(clouddrive_pb2.MultiFileRequest(path=[folder_path]), metadata=metadata)
            return f"🗑️ 清理后变为空目录，已删除: `{os.path.basename(folder_path)}`"

        if max_file_size < threshold_bytes:
            await stub.DeleteFiles(clouddrive_pb2.MultiFileRequest(path=[folder_path]), metadata=metadata)
            return f"⚠️ 最大文件 < {SIZE_THRESHOLD_MB}MB，已视为垃圾任务删除: `{os.path.basename(folder_path)}`"

        return f"✅ `{os.path.basename(folder_path)}` 检查通过。"

    except Exception as e:
        return f"❌ 处理 `{os.path.basename(folder_path)}` 异常: {str(e)}"


# --- Telegram 交互逻辑 ---
async def handle_magnet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """监听用户发送的消息，如果是 magnet/http/ed2k 则提交至离线下载"""
    if update.effective_user.id not in ADMIN_IDS: return
    text = update.message.text.strip()

    # 支持的链接前缀过滤
    if text.startswith("magnet:?xt=") or text.startswith("http") or text.startswith("ed2k://"):
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
                    await status_msg.edit_text(f"❌ CD2 拒绝请求: {response.errorMessage}")
        except Exception as e:
            await status_msg.edit_text(f"⚠️ 提交报错: {str(e)}")


async def cmd_clean(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """手动执行清理指令"""
    if update.effective_user.id not in ADMIN_IDS: return
    msg = await update.message.reply_text("🔍 正在扫描子文件夹...")
    try:
        async with grpc.aio.insecure_channel(CD2_IP_PORT) as channel:
            stub = clouddrive_pb2_grpc.CloudDriveFileSrvStub(channel)
            metadata = [('authorization', f'Bearer {CD2_TOKEN}')]
            root_req = clouddrive_pb2.ListSubFileRequest(path=SAVE_PATH)
            task_folders = []
            async for reply in stub.GetSubFiles(root_req, metadata=metadata, timeout=15):
                if reply.subFiles:
                    for f in reply.subFiles:
                        if f.isDirectory: task_folders.append(f.fullPathName)
            if not task_folders:
                await msg.edit_text("📁 下载目录为空，无需清理。")
                return
            results = [await clean_task_folder(stub, metadata, folder) for folder in task_folders]
            report = "\n".join([r for r in results if r])
            await msg.edit_text(f"📊 **清理报告：**\n{report or '扫描完成，无变动'}", parse_mode='Markdown')
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
            await update.message.reply_text(f"➕ 已添加黑名单: `{new_key}`", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"📝 当前黑名单关键词：\n`{', '.join(current)}`", parse_mode='Markdown')


async def post_init(application):
    """自动注册菜单命令至 Telegram 聊天框列表"""
    await application.bot.set_my_commands([
        BotCommand("clean", "清理下载目录下的垃圾文件夹"),
        BotCommand("blacklist", "查看或添加黑名单关键词")
    ])


# --- 程序启动入口 ---
if __name__ == '__main__':
    # 构造 Application 并应用代理配置
    builder = ApplicationBuilder().token(TG_BOT_TOKEN).post_init(post_init)

    if PROXY_URL:
        logger.info(f"启用网络代理: {PROXY_URL}")
        builder.proxy(PROXY_URL)
        builder.get_updates_proxy(PROXY_URL)

    app = builder.build()

    # 处理器注册
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_magnet))
    app.add_handler(CommandHandler("clean", cmd_clean))
    app.add_handler(CommandHandler("blacklist", cmd_black))
    app.add_handler(CommandHandler("add_black", cmd_black))

    logger.info("🚀 机器人启动成功，正在拉取消息...")
    app.run_polling()