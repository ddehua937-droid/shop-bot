import asyncio
import csv
import io
import math
import os
import time
import traceback
from datetime import datetime, timezone, timedelta

import requests
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup
)
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes
)

# ── 配置（环境变量）────────────────────────────────
BOT_TOKEN     = os.getenv("BOT_TOKEN")
SHEET_ID      = os.getenv("SHEET_ID")
MERCHANTS_GID = os.getenv("MERCHANTS_GID", "0")   # "商家" 工作表的 gid
BUTTONS_GID   = os.getenv("BUTTONS_GID", "")      # "按钮" 工作表的 gid，留空则不启用

# 访问限制：只有加了指定群的用户才能用机器人。留空 REQUIRED_GROUP_ID 则不限制任何人
REQUIRED_GROUP_ID   = os.getenv("REQUIRED_GROUP_ID", "")     # 指定群的 chat_id，例如 -1001234567890
REQUIRED_GROUP_LINK = os.getenv("REQUIRED_GROUP_LINK", "")   # 指定群的邀请链接，提示用户加入时使用
REQUIRED_GROUP_NAME = os.getenv("REQUIRED_GROUP_NAME", "指定群组")

# 使用统计：留空则不启用，/stats 会提示未配置
USAGE_LOG_URL = os.getenv("USAGE_LOG_URL", "")  # Google Apps Script Web App 网址

# /stats 只允许这些用户使用，逗号分隔多个，例如 "123456789,987654321"
ADMIN_USER_IDS = {
    uid.strip() for uid in os.getenv("ADMIN_USER_IDS", "").split(",") if uid.strip()
}

ARMENIA_TZ = timezone(timedelta(hours=4))  # 用于「今天」的日期判断

PAGE_SIZE        = 6
DELETE_AFTER     = 300   # 群里消息5分钟后自动删除
REFRESH_SECONDS  = 300   # 后台自动刷新表格间隔（5分钟）
MIN_REFRESH_GAP  = 20    # /refresh 手动刷新最小间隔（秒）
CAPTION_LIMIT    = 1024  # Telegram 图片说明文字上限

# 成员资格缓存：只缓存「是成员」的结果，避免用户加群后还要等缓存过期才能用
MEMBERSHIP_CACHE_TTL = 300  # 秒

TRUE_VALUES = {"是", "yes", "true", "1", "y", "✓", "√"}
MEMBER_STATUSES = {"creator", "administrator", "member", "restricted"}


def _csv_url(gid: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={gid}"


# ── 全局数据缓存 ───────────────────────────────────
class DataStore:
    def __init__(self):
        self.categories = []        # ["🛍️ 购物百货", "📦 快递包裹", ...] 顺序=表格首次出现顺序
        self.merchants_by_cat = {}  # {cat_idx: [{"name":.., "contact":.., "image":..}, ...]}
        self.buttons = []           # [{"label":.., "reply":.., "group_only":bool}, ...]
        self.last_refresh = 0
        self.last_error = None

    def refresh(self) -> bool:
        try:
            new_categories = []
            new_merchants = {}

            resp = requests.get(_csv_url(MERCHANTS_GID), timeout=15)
            resp.raise_for_status()
            resp.encoding = "utf-8"
            for row in csv.DictReader(io.StringIO(resp.text)):
                cat = (row.get("分类") or "").strip()
                name = (row.get("名称") or "").strip()
                contact = (row.get("详情") or "").strip()
                image = (row.get("图片") or "").strip()
                if not cat or not name:
                    continue
                if cat not in new_categories:
                    new_categories.append(cat)
                idx = new_categories.index(cat)
                new_merchants.setdefault(idx, []).append({
                    "name": name,
                    "contact": contact or "暂无详情",
                    "image": image
                })

            new_buttons = []
            if BUTTONS_GID:
                resp2 = requests.get(_csv_url(BUTTONS_GID), timeout=15)
                resp2.raise_for_status()
                resp2.encoding = "utf-8"
                for row in csv.DictReader(io.StringIO(resp2.text)):
                    label = (row.get("按钮文字") or "").strip()
                    reply = (row.get("回复内容") or "").strip()
                    group_only_raw = (row.get("仅群聊") or "").strip().lower()
                    group_only = group_only_raw in TRUE_VALUES
                    if label:
                        new_buttons.append({
                            "label": label,
                            "reply": reply or "暂无内容",
                            "group_only": group_only
                        })

            self.categories = new_categories
            self.merchants_by_cat = new_merchants
            self.buttons = new_buttons
            self.last_refresh = time.time()
            self.last_error = None
            total_merchants = sum(len(v) for v in new_merchants.values())
            print(f"[INFO] 表格已刷新：{len(new_categories)}个分类，"
                  f"{total_merchants}个商家，{len(new_buttons)}个按钮")
            return True
        except Exception as e:
            self.last_error = str(e)
            print(f"[ERROR] 表格刷新失败: {e}")
            return False


store = DataStore()


# ── 工具函数 ───────────────────────────────────────
def is_group(update: Update) -> bool:
    return update.effective_chat.type in ("group", "supergroup")


async def _do_delete(context: ContextTypes.DEFAULT_TYPE):
    try:
        await context.bot.delete_message(
            chat_id=context.job.data["chat_id"],
            message_id=context.job.data["message_id"]
        )
    except Exception:
        pass


def schedule_delete(context, chat_id, message_id):
    context.job_queue.run_once(
        _do_delete,
        when=DELETE_AFTER,
        data={"chat_id": chat_id, "message_id": message_id}
    )


async def safe_edit(query, text, reply_markup=None):
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


# ── 成员资格门禁 ───────────────────────────────────
_membership_cache = {}  # {user_id: checked_at}  —— 只记「是成员」，不记「不是成员」


async def check_membership(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not REQUIRED_GROUP_ID:
        return True  # 未配置限制，所有人都能用

    # 只信任「是成员」的缓存结果；「不是成员」从不缓存，
    # 确保用户加群后，下一次操作立刻能用，不用等缓存过期
    checked_at = _membership_cache.get(user_id)
    if checked_at and (time.time() - checked_at < MEMBERSHIP_CACHE_TTL):
        return True

    try:
        member = await context.bot.get_chat_member(chat_id=REQUIRED_GROUP_ID, user_id=user_id)
        is_member = member.status in MEMBER_STATUSES
    except Exception as e:
        print(f"[WARN] 群成员检查失败 user_id={user_id}: {e}")
        is_member = False  # 检查失败（如机器人不在该群、ID填错）时，保守拒绝

    if is_member:
        _membership_cache[user_id] = time.time()
    else:
        _membership_cache.pop(user_id, None)

    return is_member


def join_required_text() -> str:
    text = f"🔒 本机器人仅限「{REQUIRED_GROUP_NAME}」成员使用\n\n"
    if REQUIRED_GROUP_LINK:
        text += f"请先加入：{REQUIRED_GROUP_LINK}\n\n"
    else:
        text += "请先加入指定群组\n\n"
    text += "加入后重新发送 /start 即可使用"
    return text


# ── 使用统计上报 ───────────────────────────────────
_logged_today = {}  # {user_id: "YYYY-MM-DD"} —— 同一进程内每人每天只上报一次


async def log_usage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not USAGE_LOG_URL:
        return
    user = update.effective_user
    if not user:
        return

    now = datetime.now(ARMENIA_TZ)
    today_str = now.strftime("%Y-%m-%d")

    if _logged_today.get(user.id) == today_str:
        return  # 这个进程里今天已经上报过，不重复
    _logged_today[user.id] = today_str

    username = user.username or user.full_name or ""
    source = "群聊" if is_group(update) else "私聊"
    try:
        await asyncio.to_thread(
            requests.post,
            USAGE_LOG_URL,
            json={
                "date": today_str,
                "user_id": str(user.id),
                "username": username,
                "source": source,
                "time": now.strftime("%H:%M:%S"),
            },
            timeout=10
        )
    except Exception as e:
        print(f"[WARN] 使用记录上报失败 user_id={user.id}: {e}")


# ── 统一渲染：自动处理 文字⇄图片 之间的切换 ──────────
async def render(query, context, text, reply_markup=None, photo_url=None):
    """
    text 屏幕和图片屏幕之间无法直接编辑切换，所以：
    - 文字 -> 文字：原地编辑（不闪烁）
    - 涉及图片的任何切换：删除旧消息 + 发新消息
    """
    chat_id = query.message.chat_id
    is_photo_msg = bool(query.message.photo)

    if not photo_url and not is_photo_msg:
        await safe_edit(query, text, reply_markup=reply_markup)
        return

    try:
        await query.message.delete()
    except Exception:
        pass

    sent = None
    if photo_url:
        caption = text
        if caption and len(caption) > CAPTION_LIMIT:
            caption = caption[:CAPTION_LIMIT - 20] + "\n…（内容过长已截断）"
        try:
            sent = await context.bot.send_photo(
                chat_id=chat_id, photo=photo_url, caption=caption, reply_markup=reply_markup
            )
        except BadRequest:
            sent = await context.bot.send_message(
                chat_id=chat_id, text=text + "\n\n⚠️ 图片加载失败", reply_markup=reply_markup
            )
    else:
        sent = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)

    if sent and chat_id and query.message.chat.type in ("group", "supergroup"):
        schedule_delete(context, chat_id, sent.message_id)


# ── 键盘构建（全部来自 store）──────────────────────
# 群聊：只显示「仅群聊」按钮（分类、普通按钮一律不显示）
# 私聊：显示分类 + 非「仅群聊」按钮
def reply_menu(is_group_chat: bool) -> ReplyKeyboardMarkup:
    rows, row = [], []

    if is_group_chat:
        for b in store.buttons:
            if not b.get("group_only"):
                continue
            row.append(b["label"])
            if len(row) == 2:
                rows.append(row); row = []
        if row:
            rows.append(row)
    else:
        for cat in store.categories:
            row.append(cat)
            if len(row) == 2:
                rows.append(row); row = []
        if row:
            rows.append(row); row = []
        for b in store.buttons:
            if b.get("group_only"):
                continue
            row.append(b["label"])
            if len(row) == 2:
                rows.append(row); row = []
        if row:
            rows.append(row)

    if not rows:
        rows = [["⏳ 数据加载中，请稍后"]]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    rows, row = [], []
    for idx, cat in enumerate(store.categories):
        row.append(InlineKeyboardButton(cat, callback_data=f"C:{idx}:0"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    if not rows:
        rows = [[InlineKeyboardButton("⏳ 数据加载中", callback_data="noop")]]
    return InlineKeyboardMarkup(rows)


def merchant_keyboard(cat_idx: int, page: int) -> InlineKeyboardMarkup:
    items = store.merchants_by_cat.get(cat_idx, [])
    total_pages = max(1, math.ceil(len(items) / PAGE_SIZE))
    page = page % total_pages
    start = page * PAGE_SIZE
    page_items = items[start:start + PAGE_SIZE]

    keyboard = []
    for row_start in range(0, len(page_items), 2):
        row = []
        for local_i in range(row_start, min(row_start + 2, len(page_items))):
            global_idx = start + local_i
            item = page_items[local_i]
            row.append(InlineKeyboardButton(
                item["name"],
                callback_data=f"M:{cat_idx}:{global_idx}"
            ))
        keyboard.append(row)

    keyboard.append([
        InlineKeyboardButton(
            f"🔄 换一批 ({page + 1}/{total_pages})",
            callback_data=f"C:{cat_idx}:{page + 1}"
        ),
        InlineKeyboardButton("🏠 主菜单", callback_data="main"),
    ])
    return InlineKeyboardMarkup(keyboard)


def detail_keyboard(cat_idx: int, page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 返回列表", callback_data=f"C:{cat_idx}:{page}"),
        InlineKeyboardButton("🏠 主菜单", callback_data="main"),
    ]])


# ── /start ─────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await check_membership(user_id, context):
        await update.message.reply_text(join_required_text())
        return

    await log_usage(update, context)

    in_group = is_group(update)
    greeting = "📢 欢迎，点击下方按钮👇" if in_group else "📢 功能导航，请选择👇"
    msg = await update.message.reply_text(
        greeting,
        reply_markup=reply_menu(in_group)
    )
    if in_group:
        schedule_delete(context, msg.chat_id, msg.message_id)


# ── /groupid（查看当前会话ID，方便配置 REQUIRED_GROUP_ID）──
async def groupid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await update.message.reply_text(
        f"当前会话 ID：{chat.id}\n类型：{chat.type}"
    )


# ── /stats（查看使用统计，仅限管理员）─────────────────
async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if ADMIN_USER_IDS and user_id not in ADMIN_USER_IDS:
        await update.message.reply_text("⛔ 该指令仅限管理员使用")
        return

    if not USAGE_LOG_URL:
        await update.message.reply_text("⚠️ 还没配置使用统计功能（缺少 USAGE_LOG_URL）")
        return

    today_str = datetime.now(ARMENIA_TZ).strftime("%Y-%m-%d")
    try:
        resp = await asyncio.to_thread(
            requests.get, USAGE_LOG_URL, params={"date": today_str}, timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        await update.message.reply_text(f"❌ 读取统计数据失败：{e}")
        return

    await update.message.reply_text(
        f"📊 使用统计\n\n"
        f"今天（{today_str}）使用人数：{data.get('today_count', '?')} 人\n"
        f"累计使用人数：{data.get('total_count', '?')} 人"
    )


# ── /refresh（手动刷新表格）─────────────────────────
async def refresh_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = time.time()
    if now - store.last_refresh < MIN_REFRESH_GAP:
        await update.message.reply_text("⏳ 刚刷新过，请稍后再试")
        return
    ok = await asyncio.to_thread(store.refresh)
    if ok:
        total = sum(len(v) for v in store.merchants_by_cat.values())
        await update.message.reply_text(
            f"✅ 已刷新：{len(store.categories)}个分类，"
            f"{total}个商家，{len(store.buttons)}个按钮"
        )
    else:
        await update.message.reply_text(f"❌ 刷新失败：{store.last_error}")


# ── 底部键盘处理 ───────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await check_membership(user_id, context):
        await update.message.reply_text(join_required_text())
        return

    await log_usage(update, context)

    text = update.message.text
    in_group = is_group(update)

    # 分类菜单只在私聊可用
    if not in_group and text in store.categories:
        cat_idx = store.categories.index(text)
        await update.message.reply_text(
            f"{text}，点击查看详情👇",
            reply_markup=merchant_keyboard(cat_idx, 0)
        )
        return

    for b in store.buttons:
        g_only = b.get("group_only")
        if in_group and not g_only:
            continue
        if not in_group and g_only:
            continue
        if text == b["label"]:
            msg = await update.message.reply_text(b["reply"])
            if in_group:
                schedule_delete(context, msg.chat_id, msg.message_id)
            return


# ── Inline 按钮回调 ────────────────────────────────
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    user_id = query.from_user.id
    if not await check_membership(user_id, context):
        await query.answer("🔒 请先加入指定群组才能使用", show_alert=True)
        return

    await log_usage(update, context)

    try:
        if data == "noop":
            await query.answer("数据加载中，请稍后再试", show_alert=True)
            return

        if data == "main":
            await query.answer()
            await render(query, context, "📢 请选择分类👇", reply_markup=main_menu_keyboard())
            return

        parts = data.split(":", 2)

        # ── 分类列表 ──
        if parts[0] == "C" and len(parts) == 3:
            cat_idx = int(parts[1])
            page = int(parts[2])
            if cat_idx >= len(store.categories):
                await query.answer("分类已更新，请返回主菜单", show_alert=True)
                return
            title = store.categories[cat_idx]
            await query.answer()
            items = store.merchants_by_cat.get(cat_idx, [])
            if not items:
                await render(
                    query, context,
                    f"{title}\n\n暂无内容，敬请期待！",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 返回", callback_data="main")
                    ]])
                )
                return
            await render(
                query, context,
                f"{title}，点击查看详情👇",
                reply_markup=merchant_keyboard(cat_idx, page)
            )
            return

        # ── 商家详情 ──
        if parts[0] == "M" and len(parts) == 3:
            cat_idx = int(parts[1])
            idx = int(parts[2])
            items = store.merchants_by_cat.get(cat_idx, [])
            if cat_idx >= len(store.categories) or idx >= len(items):
                await query.answer("该信息不存在，请返回主菜单", show_alert=True)
                return
            merchant = items[idx]
            page = idx // PAGE_SIZE
            title = store.categories[cat_idx]
            await query.answer()
            text = (
                f"📋 {merchant['name']}\n\n"
                f"🔎 详情：\n{merchant['contact']}\n\n"
                f"来自 {title}"
            )
            await render(
                query, context, text,
                reply_markup=detail_keyboard(cat_idx, page),
                photo_url=merchant.get("image") or None
            )
            return

        await query.answer("未知操作", show_alert=True)

    except Exception as e:
        print(f"[ERROR] button_handler 异常:\n{traceback.format_exc()}")
        try:
            await query.answer(f"出错: {e}", show_alert=True)
        except Exception:
            pass


# ── 后台定时刷新 ───────────────────────────────────
async def _periodic_refresh(context: ContextTypes.DEFAULT_TYPE):
    await asyncio.to_thread(store.refresh)


# ── 启动 ───────────────────────────────────────────
def main():
    if not BOT_TOKEN:
        raise RuntimeError("缺少环境变量 BOT_TOKEN")
    if not SHEET_ID:
        raise RuntimeError("缺少环境变量 SHEET_ID")

    store.refresh()  # 启动时先加载一次

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("refresh", refresh_cmd))
    app.add_handler(CommandHandler("groupid", groupid_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_handler))

    app.job_queue.run_repeating(
        _periodic_refresh,
        interval=REFRESH_SECONDS,
        first=REFRESH_SECONDS
    )

    print("✅ 机器人启动成功")
    if REQUIRED_GROUP_ID:
        print(f"🔒 已启用群成员门槛，要求加入: {REQUIRED_GROUP_ID}")
    app.run_polling()


if __name__ == "__main__":
    main()
