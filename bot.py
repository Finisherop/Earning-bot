#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram User Bot (python-telegram-bot v20+) + Firebase Firestore (Admin SDK)

Features:
- /start with optional referral payload, user auto-register
- Inline menus for actions
- 🎬 Watch Ads (random reward × VIP multiplier)
- 🎁 Daily Bonus (once per day × VIP multiplier)
- 👥 Refer & Earn (deep link + current referral count)
- 💸 Balance (coins, VIP, total withdrawals) + 🏦 Withdraw flow (UPI, amount, validations)
- ✨ Extra (VIP Plans, Stats, Support)
- 👑 VIP Plans via Telegram Stars invoices (XTR)
- 📊 Stats (referrals, adsWatched, coins, VIP, withdrawals)
- 🆘 Support link from Firestore config/global
- Firestore helper functions and server timestamps
"""

import os
import random
import logging
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv

from telegram import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Update,
    LabeledPrice,
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    filters,
    ContextTypes,
    PreCheckoutQueryHandler,
)

# ----------------- Logging -----------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ----------------- Env -----------------
load_dotenv()
BOT_TOKEN = os.getenv("USER_BOT_TOKEN")
FIREBASE_CRED_PATH = os.getenv("FIREBASE_CRED_PATH")

if not BOT_TOKEN or not FIREBASE_CRED_PATH:
    raise RuntimeError(
        "Missing env vars. Ensure USER_BOT_TOKEN and FIREBASE_CRED_PATH are set in .env"
    )

# ----------------- Firebase Admin -----------------
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore import FieldValue  # type: ignore

cred = credentials.Certificate(FIREBASE_CRED_PATH)
firebase_admin.initialize_app(cred)
db = firestore.client()

# ----------------- Constants -----------------
CB_WATCH_ADS = "watch_ads"
CB_BONUS = "daily_bonus"
CB_REFER = "refer"
CB_BALANCE = "balance"
CB_WITHDRAW = "withdraw"
CB_EXTRA = "extra"
CB_VIP_PLANS = "vip_plans"
CB_STATS = "stats"
CB_SUPPORT = "support"

CB_VIP1_BUY = "vip1_buy"
CB_VIP2_BUY = "vip2_buy"
CB_VIP3_BUY = "vip3_buy"

# Conversation states for Withdraw Flow
ASK_UPI, ASK_AMOUNT = range(2)

# ----------------- Utilities -----------------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def date_utc(d: datetime) -> datetime.date:
    return d.astimezone(timezone.utc).date()

def safe_int(v, default=0) -> int:
    try:
        return int(v)
    except Exception:
        return default

# ----------------- Firestore Helpers -----------------
def users_col():
    return db.collection("users")

def withdrawals_col():
    return db.collection("withdrawals")

def config_doc():
    return db.collection("config").document("global")

def get_config() -> dict:
    snap = config_doc().get()
    if not snap.exists:
        # sensible defaults to avoid crashes (you can edit config/global in Firestore later)
        return {
            "referralReward": 50,
            "bonusReward": 100,
            "adRewardMin": 5,
            "adRewardMax": 25,
            "adWebsiteURL": "https://example.com",
            "supportBot": "https://t.me/example_support",
            "minRefForWithdraw": 0,
            "vipCosts": {"vip1": 50, "vip2": 200, "vip3": 500},  # XTR (Telegram Stars)
            "vipMultipliers": {"vip1": 1.2, "vip2": 1.5, "vip3": 2.0},
        }
    data = snap.to_dict() or {}
    # Ensure nested dicts exist
    data.setdefault("vipCosts", {"vip1": 50, "vip2": 200, "vip3": 500})
    data.setdefault("vipMultipliers", {"vip1": 1.2, "vip2": 1.5, "vip3": 2.0})
    return data

def get_user(uid: int) -> dict | None:
    doc = users_col().document(str(uid)).get()
    return doc.to_dict() if doc.exists else None

def add_user(uid: int, name: str, ref_by: int | None) -> dict:
    user_doc = users_col().document(str(uid))
    base = {
        "id": uid,
        "name": name,
        "coins": 0,
        "reffer": 0,  # total successful referrals count
        "refferBy": ref_by if ref_by else None,
        "adsWatched": 0,
        "tasksCompleted": 0,
        "totalWithdrawals": 0,
        "withdrawalsDone": 0,
        "vipTier": "none",
        "vipActivatedAt": None,
        "joinedAt": firestore.SERVER_TIMESTAMP,
        "lastBonusAt": None,
        "banned": False,
    }
    user_doc.set(base, merge=True)
    return base

def update_user(uid: int, data: dict) -> None:
    users_col().document(str(uid)).set(data, merge=True)

def increment_user(uid: int, field: str, amount: int | float):
    users_col().document(str(uid)).update({field: FieldValue.increment(amount)})

def set_server_ts(uid: int, field: str):
    users_col().document(str(uid)).update({field: firestore.SERVER_TIMESTAMP})

def count_referrals(uid: int) -> int:
    # count users where refferBy == uid
    q = users_col().where("refferBy", "==", uid).select([]).stream()
    return sum(1 for _ in q)

def create_withdrawal(uid: int, upi: str, amount: int) -> str:
    doc_ref = withdrawals_col().document()
    doc_ref.set(
        {
            "userId": uid,
            "upi": upi,
            "amount": amount,
            "status": "pending",
            "requestedAt": firestore.SERVER_TIMESTAMP,
            "processedAt": None,
        }
    )
    # Update aggregates
    increment_user(uid, "totalWithdrawals", amount)
    increment_user(uid, "withdrawalsDone", 1)
    return doc_ref.id

# ----------------- Rewards & VIP helpers -----------------
def get_vip_multiplier(user: dict, cfg: dict) -> float:
    tier = (user.get("vipTier") or "none").lower()
    multipliers = cfg.get("vipMultipliers", {})
    if tier in multipliers:
        return float(multipliers[tier])
    return 1.0

def calc_ad_reward(user: dict, cfg: dict) -> int:
    rmin = safe_int(cfg.get("adRewardMin", 5), 5)
    rmax = safe_int(cfg.get("adRewardMax", 25), 25)
    base = random.randint(min(rmin, rmax), max(rmin, rmax))
    reward = int(round(base * get_vip_multiplier(user, cfg)))
    return max(reward, 0)

def calc_bonus_reward(user: dict, cfg: dict) -> int:
    base = safe_int(cfg.get("bonusReward", 100), 100)
    reward = int(round(base * get_vip_multiplier(user, cfg)))
    return max(reward, 0)

# ----------------- UI -----------------
def main_menu() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("🎬 Watch Ads", callback_data=CB_WATCH_ADS),
            InlineKeyboardButton("🎁 Bonus", callback_data=CB_BONUS),
        ],
        [
            InlineKeyboardButton("👥 Refer & Earn", callback_data=CB_REFER),
            InlineKeyboardButton("💸 Balance", callback_data=CB_BALANCE),
        ],
        [
            InlineKeyboardButton("✨ Extra", callback_data=CB_EXTRA),
        ],
    ]
    return InlineKeyboardMarkup(rows)

def extra_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("👑 VIP Plans", callback_data=CB_VIP_PLANS),
                InlineKeyboardButton("📊 Stats", callback_data=CB_STATS),
            ],
            [InlineKeyboardButton("🆘 Support", callback_data=CB_SUPPORT)],
            [InlineKeyboardButton("⬅️ Back", callback_data="back_main")],
        ]
    )

def balance_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🏦 Withdraw Funds", callback_data=CB_WITHDRAW)],
            [InlineKeyboardButton("⬅️ Back", callback_data="back_main")],
        ]
    )

def vip_menu(cfg: dict) -> InlineKeyboardMarkup:
    costs = cfg.get("vipCosts", {})
    m = cfg.get("vipMultipliers", {})
    def label(tier):
        return f"{tier.upper()} — {costs.get(tier, '?')} XTR ×{m.get(tier, 1)}"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(label("vip1"), callback_data=CB_VIP1_BUY)],
            [InlineKeyboardButton(label("vip2"), callback_data=CB_VIP2_BUY)],
            [InlineKeyboardButton(label("vip3"), callback_data=CB_VIP3_BUY)],
            [InlineKeyboardButton("⬅️ Back", callback_data="back_extra")],
        ]
    )

# ----------------- Core Handlers -----------------
async def ensure_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> dict:
    """Create user if not exists. Apply referral if deep link used."""
    assert update.effective_user
    u = update.effective_user
    uid = u.id
    name = (u.full_name or u.username or str(uid)).strip()

    user = get_user(uid)
    if user:
        return user

    # parse referral from /start payload (if any)
    ref_by = None
    if update.message and update.message.text:
        parts = update.message.text.split(maxsplit=1)
        if len(parts) == 2:
            candidate = safe_int(parts[1], 0)
            if candidate and candidate != uid:
                ref_by = candidate

    user = add_user(uid, name, ref_by)

    cfg = get_config()
    referral_reward = safe_int(cfg.get("referralReward", 50), 50)

    # Give reward to new user if there was a valid referrer
    if ref_by:
        try:
            ref_user = get_user(ref_by)
            if ref_user:
                # Reward both sides, increment referrer count
                increment_user(uid, "coins", referral_reward)
                increment_user(ref_by, "coins", referral_reward)
                increment_user(ref_by, "reffer", 1)
        except Exception as e:
            logger.error(f"Referral grant failed: {e}")

    # fetch final user snapshot
    return get_user(uid) or user

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await ensure_user(update, context)

    if user.get("banned"):
        await update.effective_message.reply_text("🚫 You are banned.")
        return

    me = await context.bot.get_me()
    greet = (
        f"Hey {user.get('name','there')}!\n"
        f"Welcome to *Earning Hub* 💸\n\n"
        f"Use the menu below to earn coins, claim bonus, and more."
    )
    await update.effective_message.reply_text(
        greet, parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu()
    )

# ------------- Callback: Back buttons -------------
async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("Main Menu:", reply_markup=main_menu())

async def back_to_extra(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("✨ Extra:", reply_markup=extra_menu())

# ------------- Watch Ads -------------
async def on_watch_ads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    user = get_user(update.effective_user.id)
    if not user or user.get("banned"):
        await q.edit_message_text("🚫 You are banned or not registered.")
        return

    cfg = get_config()
    reward = calc_ad_reward(user, cfg)

    try:
        # Example flow: user clicks, we "open" ad URL + grant reward
        ad_url = cfg.get("adWebsiteURL") or "https://example.com"
        increment_user(user["id"], "coins", reward)
        increment_user(user["id"], "adsWatched", 1)
        await q.edit_message_text(
            f"🎬 Ad watched!\n\nYou earned *{reward}* coins.\nTap again to watch more.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("Open Ad Website", url=ad_url)],
                    [InlineKeyboardButton("⬅️ Back", callback_data="back_main")],
                ]
            ),
        )
    except Exception as e:
        logger.exception(e)
        await q.edit_message_text("Something went wrong. Please try again later.")

# ------------- Daily Bonus -------------
async def on_bonus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    user = get_user(update.effective_user.id)
    if not user or user.get("banned"):
        await q.edit_message_text("🚫 You are banned or not registered.")
        return

    cfg = get_config()
    last = user.get("lastBonusAt")
    today = date_utc(now_utc())

    eligible = False
    if not last:
        eligible = True
    else:
        try:
            # Firestore timestamp -> datetime
            last_dt = last if isinstance(last, datetime) else last.to_datetime()
            eligible = date_utc(last_dt) < today
        except Exception:
            eligible = True

    if not eligible:
        await q.edit_message_text(
            "⏳ Daily bonus already claimed today.\nCome back tomorrow!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back_main")]]),
        )
        return

    reward = calc_bonus_reward(user, cfg)
    try:
        increment_user(user["id"], "coins", reward)
        set_server_ts(user["id"], "lastBonusAt")
        await q.edit_message_text(
            f"🎁 Bonus claimed!\nYou received *{reward}* coins.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back_main")]]),
        )
    except Exception as e:
        logger.exception(e)
        await q.edit_message_text("Couldn't grant bonus. Try again later.")

# ------------- Refer & Earn -------------
async def on_refer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    me = await context.bot.get_me()
    user = get_user(update.effective_user.id)
    if not user:
        await q.edit_message_text("Please /start first.")
        return

    refs = count_referrals(user["id"])
    deep_link = f"https://t.me/{me.username}?start={user['id']}"

    text = (
        "👥 *Refer & Earn*\n\n"
        f"Share your link:\n`{deep_link}`\n\n"
        f"Current referrals: *{refs}*"
    )
    await q.edit_message_text(
        text, parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back_main")]])
    )

# ------------- Balance -------------
async def on_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    user = get_user(update.effective_user.id)
    if not user:
        await q.edit_message_text("Please /start first.")
        return

    text = (
        "💸 *Your Balance*\n\n"
        f"Coins: *{user.get('coins',0)}*\n"
        f"VIP Tier: *{user.get('vipTier','none').upper()}*\n"
        f"Total Withdrawals: *{user.get('totalWithdrawals',0)}*\n"
        f"Withdrawals Done: *{user.get('withdrawalsDone',0)}*"
    )
    await q.edit_message_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=balance_menu(),
    )

# ------------- Withdraw Flow -------------
async def start_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    user = get_user(update.effective_user.id)
    if not user:
        await q.edit_message_text("Please /start first.")
        return ConversationHandler.END

    cfg = get_config()
    min_ref = safe_int(cfg.get("minRefForWithdraw", 0), 0)
    your_refs = count_referrals(user["id"])

    if user.get("coins", 0) <= 0:
        await q.edit_message_text(
            "You have 0 coins. Earn some coins before withdrawing.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back_main")]]),
        )
        return ConversationHandler.END

    if your_refs < min_ref:
        await q.edit_message_text(
            f"❗ You need at least *{min_ref}* referrals to withdraw. "
            f"Current: *{your_refs}*.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back_main")]]),
        )
        return ConversationHandler.END

    context.user_data["wd"] = {}
    await q.edit_message_text(
        "🏦 Withdraw: Please send your *UPI ID* (e.g., `name@bank`).",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ASK_UPI

async def ask_upi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upi = (update.effective_message.text or "").strip()
    if not upi or "@" not in upi or len(upi) < 5:
        await update.effective_message.reply_text(
            "That UPI looks invalid. Send again (e.g., `name@bank`).",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ASK_UPI

    context.user_data["wd"]["upi"] = upi
    user = get_user(update.effective_user.id)
    await update.effective_message.reply_text(
        f"Great. Your current coins: *{user.get('coins',0)}*.\n"
        "Now send the *amount* you want to withdraw (as a number).",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ASK_AMOUNT

async def ask_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amt = safe_int((update.effective_message.text or "").strip(), -1)
    user = get_user(update.effective_user.id)

    if amt <= 0:
        await update.effective_message.reply_text("Amount must be a positive number. Try again:")
        return ASK_AMOUNT

    coins = user.get("coins", 0)
    if amt > coins:
        await update.effective_message.reply_text(
            f"You only have {coins} coins. Send an amount ≤ {coins}."
        )
        return ASK_AMOUNT

    upi = context.user_data.get("wd", {}).get("upi")
    if not upi:
        await update.effective_message.reply_text("Session expired. Start again from Balance → Withdraw.")
        return ConversationHandler.END

    # Deduct coins and create withdrawal atomically via transaction
    def tx_op(transaction, user_ref):
        snapshot = user_ref.get(transaction=transaction)
        cur = snapshot.get("coins") or 0
        if cur < amt:
            raise ValueError("Insufficient coins during transaction.")
        transaction.update(user_ref, {"coins": cur - amt})

    try:
        user_ref = users_col().document(str(user["id"]))
        db.transaction()(tx_op)(user_ref)
    except Exception as e:
        logger.exception(e)
        await update.effective_message.reply_text("Could not deduct coins. Please try again later.")
        return ConversationHandler.END

    # Create withdrawal document and bump aggregates
    try:
        doc_id = create_withdrawal(user["id"], upi, amt)
    except Exception as e:
        logger.exception(e)
        # refund on failure
        increment_user(user["id"], "coins", amt)
        await update.effective_message.reply_text("Failed to create withdrawal. Your coins were refunded.")
        return ConversationHandler.END

    await update.effective_message.reply_text(
        f"✅ Withdrawal request created!\n\nID: `{doc_id}`\nUPI: `{upi}`\nAmount: *{amt}* coins\n\n"
        "Status: *pending*. We'll process it soon.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_main")]]),
    )
    context.user_data.pop("wd", None)
    return ConversationHandler.END

async def withdraw_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Withdraw cancelled.", reply_markup=main_menu())
    return ConversationHandler.END

# ------------- Extra -------------
async def on_extra(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("✨ Extra:", reply_markup=extra_menu())

# ------------- VIP Plans (Telegram Stars) -------------
async def on_vip_plans(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cfg = get_config()
    await q.edit_message_text("👑 VIP Plans:", reply_markup=vip_menu(cfg))

async def _send_vip_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE, tier: str):
    q = update.callback_query
    await q.answer()
    cfg = get_config()
    costs = cfg.get("vipCosts", {})
    amount_xtr = safe_int(costs.get(tier, 0), 0)
    if amount_xtr <= 0:
        await q.edit_message_text("VIP plan unavailable right now.")
        return

    title = f"{tier.upper()} VIP"
    desc = f"Unlock {tier.upper()} VIP benefits. Multiplier ×{cfg.get('vipMultipliers',{}).get(tier,1)}"
    payload = f"VIP::{tier}"
    currency = "XTR"  # Telegram Stars
    prices = [LabeledPrice(label=title, amount=amount_xtr)]  # amount in XTR (stars)

    await context.bot.send_invoice(
        chat_id=update.effective_chat.id,
        title=title,
        description=desc,
        payload=payload,
        provider_token="",  # not required for Stars
        currency=currency,
        prices=prices,
        protect_content=True,
    )

async def on_vip1_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_vip_invoice(update, context, "vip1")

async def on_vip2_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_vip_invoice(update, context, "vip2")

async def on_vip3_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_vip_invoice(update, context, "vip3")

async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    # For Stars, we simply approve
    await query.answer(ok=True)

async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    sp = msg.successful_payment
    payload = sp.invoice_payload  # "VIP::vip1"
    tier = None
    if payload and payload.startswith("VIP::"):
        tier = payload.split("::", 1)[1]

    if not tier:
        await msg.reply_text("Payment received, but could not assign VIP. Contact support.")
        return

    uid = update.effective_user.id
    update_user(uid, {"vipTier": tier, "vipActivatedAt": firestore.SERVER_TIMESTAMP})
    await msg.reply_text(
        f"🎉 VIP upgraded to *{tier.upper()}*! Enjoy boosted rewards.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu(),
    )

# ------------- Stats -------------
async def on_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    user = get_user(update.effective_user.id)
    if not user:
        await q.edit_message_text("Please /start first.")
        return

    refs = count_referrals(user["id"])
    text = (
        "📊 *Your Stats*\n\n"
        f"Referrals: *{refs}*\n"
        f"Ads Watched: *{user.get('adsWatched',0)}*\n"
        f"Coins: *{user.get('coins',0)}*\n"
        f"VIP Tier: *{user.get('vipTier','none').upper()}*\n"
        f"Total Withdrawals: *{user.get('totalWithdrawals',0)}*"
    )
    await q.edit_message_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back_extra")]]),
    )

# ------------- Support -------------
async def on_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cfg = get_config()
    link = cfg.get("supportBot") or "https://t.me/"
    await q.edit_message_text(
        "🆘 Need help? Tap Support:",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Open Support", url=link)],
                [InlineKeyboardButton("⬅️ Back", callback_data="back_extra")],
            ]
        ),
    )

# ------------- Unknown Text (helper to keep UX clean) -------------
async def fallback_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Use the menu below:", reply_markup=main_menu())

# ----------------- App Setup -----------------
def build_application():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))

    # Withdraw conversation
    withdraw_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_withdraw, pattern=f"^{CB_WITHDRAW}$")],
        states={
            ASK_UPI: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_upi)],
            ASK_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_amount)],
        },
        fallbacks=[CommandHandler("cancel", withdraw_cancel)],
        allow_reentry=True,
    )
    app.add_handler(withdraw_conv)

    # Callbacks
    app.add_handler(CallbackQueryHandler(on_watch_ads, pattern=f"^{CB_WATCH_ADS}$"))
    app.add_handler(CallbackQueryHandler(on_bonus, pattern=f"^{CB_BONUS}$"))
    app.add_handler(CallbackQueryHandler(on_refer, pattern=f"^{CB_REFER}$"))
    app.add_handler(CallbackQueryHandler(on_balance, pattern=f"^{CB_BALANCE}$"))
    app.add_handler(CallbackQueryHandler(on_extra, pattern=f"^{CB_EXTRA}$"))
    app.add_handler(CallbackQueryHandler(on_vip_plans, pattern=f"^{CB_VIP_PLANS}$"))
    app.add_handler(CallbackQueryHandler(on_stats, pattern=f"^{CB_STATS}$"))
    app.add_handler(CallbackQueryHandler(on_support, pattern=f"^{CB_SUPPORT}$"))
    app.add_handler(CallbackQueryHandler(back_to_main, pattern="^back_main$"))
    app.add_handler(CallbackQueryHandler(back_to_extra, pattern="^back_extra$"))

    # VIP invoices (Telegram Stars)
    app.add_handler(CallbackQueryHandler(on_vip1_buy, pattern=f"^{CB_VIP1_BUY}$"))
    app.add_handler(CallbackQueryHandler(on_vip2_buy, pattern=f"^{CB_VIP2_BUY}$"))
    app.add_handler(CallbackQueryHandler(on_vip3_buy, pattern=f"^{CB_VIP3_BUY}$"))
    app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))

    # Fallback
    app.add_handler(MessageHandler(filters.COMMAND, fallback_text))
    app.add_handler(MessageHandler(filters.ALL, fallback_text))

    return app

def main():
    app = build_application()
    logger.info("Bot is starting...")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
