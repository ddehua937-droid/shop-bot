import asyncio
import csv
import io
import math
import os
import time
import traceback

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

PAGE_SIZE        = 6
DELETE_AFTER     = 300   # 群里消息5分钟后自动删除
REFRESH_SECONDS  = 300   # 后台自动刷新表格间隔（5分钟）
MIN_REFRESH_GAP  = 20    # /refresh 手动刷新最小间隔（秒）
CAPTION_LIMIT    = 1024  # Telegram 图片说明文字上限

TRUE_VALUES = {"是", "yes", "true", "1", "y", "✓", "√"}


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
    in_group = is_group(update)
    greeting = "📢 欢迎，点击下方按钮👇" if in_group else "📢 功能导航，请选择👇"
    msg = await update.message.reply_text(
        greeting,
        reply_markup=reply_menu(in_group)
    )
    if in_group:
        schedule_delete(context, msg.chat_id, msg.message_id)


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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_handler))

    app.job_queue.run_repeating(
        _periodic_refresh,
        interval=REFRESH_SECONDS,
        first=REFRESH_SECONDS
    )

    print("✅ 机器人启动成功")
    app.run_polling()


if __name__ == "__main__":
    main()
