# -*- coding: utf-8 -*-

"""
A Telegram Bot to manage channels with a robust Owner/Admin role system,
mandatory join, and block detection. Features automatic banning from free channels.
"""

import logging
import json
import os
from telegram import Update, ChatMember, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatType
from telegram.error import Forbidden
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, ChatMemberHandler, CallbackQueryHandler, MessageHandler, filters, JobQueue
# --- KEEPALIVE WEB SERVER (for Heroku/Koyeb) ---
import threading
try:
    from flask import Flask
    def _start_keepalive():
        port = int(os.environ.get("PORT", "0") or "0")
        if port:
            app = Flask(__name__)
            @app.get("/")
            def _index():
                return "OK", 200
            @app.get("/health")
            def _health():
                return "ok", 200
            th = threading.Thread(target=lambda: app.run(host="0.0.0.0", port=port, use_reloader=False), daemon=True)
            th.start()
except Exception:
    def _start_keepalive():
        pass

_start_keepalive()
# [AUTOCALL]


# --- CONFIGURATION SECTION ---
# --- Render Environment Variables se jankari lein ---

# 1. Telegram Bot Token from BotFather
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN environment variable")

# 2. Bot Owner User ID (Full Control)
OWNER_ID = int(os.environ.get("OWNER_ID", 0))

# 3. List of Admin User IDs (Can manage channels and post)
admin_ids_str = os.environ.get("ADMIN_IDS", "")
ADMIN_IDS = [int(admin_id.strip()) for admin_id in admin_ids_str.split(',') if admin_id.strip()]

# 4. Mandatory Channel Chat ID
MANDATORY_CHANNEL_ID = int(os.environ.get("MANDATORY_CHANNEL_ID", 0))

# 5. Mandatory Channel Invite Link
MANDATORY_CHANNEL_LINK = os.environ.get("MANDATORY_CHANNEL_LINK")

# 6. Contact Bot/User Link
CONTACT_ADMIN_LINK = os.environ.get("CONTACT_ADMIN_LINK")

# 7. Channel to log bot blocks
LOG_CHANNEL_ID = int(os.environ.get("LOG_CHANNEL_ID", 0))

# 8. Data file for persistence (Render Disk Path)
DATA_FILE = os.environ.get("DATA_FILE") or ("/data/bot_data.json" if os.path.exists("/data") else "bot_data.json")

# --- DYNAMIC DATA (Loaded from file) ---
FREE_CHANNELS = {}
FREE_CHANNEL_LINKS = {}
PAID_CHANNELS = []
USER_DATA = {}
BLOCKED_USER_IDS = set()
ACTIVE_CHATS = {}

# --- END OF CONFIGURATION ---

# Add Owner to Admin list automatically
if OWNER_ID and OWNER_ID not in ADMIN_IDS:
    ADMIN_IDS.append(OWNER_ID)

# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)


# --- DATA PERSISTENCE ---
def save_data():
    """Saves the current bot data to a JSON file."""
    try:
        # FIX: Ensure the directory exists before writing the file
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        data = {
            "ADMIN_IDS": ADMIN_IDS,
            "FREE_CHANNELS": FREE_CHANNELS,
            "FREE_CHANNEL_LINKS": FREE_CHANNEL_LINKS,
            "PAID_CHANNELS": PAID_CHANNELS,
            "BLOCKED_USER_IDS": list(BLOCKED_USER_IDS),
            "ACTIVE_CHATS": ACTIVE_CHATS
        }
        with open(DATA_FILE, "w") as f:
            json.dump(data, f, indent=4)
        logger.info("Data saved successfully.")
    except Exception as e:
        logger.error(f"Error saving data: {e}")

def load_data():
    """Loads bot data from a JSON file on startup."""
    global ADMIN_IDS, FREE_CHANNELS, FREE_CHANNEL_LINKS, PAID_CHANNELS, BLOCKED_USER_IDS, ACTIVE_CHATS
    try:
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
            ADMIN_IDS = data.get("ADMIN_IDS", ADMIN_IDS)
            FREE_CHANNELS = {int(k): v for k, v in data.get("FREE_CHANNELS", {}).items()}
            FREE_CHANNEL_LINKS = {int(k): v for k, v in data.get("FREE_CHANNEL_LINKS", {}).items()}
            PAID_CHANNELS = data.get("PAID_CHANNELS", [])
            BLOCKED_USER_IDS = set(data.get("BLOCKED_USER_IDS", []))
            ACTIVE_CHATS = {int(k): v for k, v in data.get("ACTIVE_CHATS", {}).items()}
            logger.info("Data loaded successfully.")
    except FileNotFoundError:
        logger.warning("Data file not found. Using default values and creating a new file.")
        save_data()
    except Exception as e:
        logger.error(f"Error loading data: {e}")

# --- PERMISSION CHECKS ---
def is_owner(user_id: int) -> bool: return user_id == OWNER_ID
def is_admin(user_id: int) -> bool: return user_id in ADMIN_IDS


# --- HELPER FUNCTIONS ---
async def is_user_member_of_channel(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if is_admin(user_id): return True
    try:
        member = await context.bot.get_chat_member(chat_id=MANDATORY_CHANNEL_ID, user_id=user_id)
        return member.status in [ChatMember.OWNER, ChatMember.ADMINISTRATOR, ChatMember.MEMBER]
    except Exception as e:
        logger.error(f"Error checking membership for {user_id} in {MANDATORY_CHANNEL_ID}: {e}")
        return False

async def remove_user_from_free_channels(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Kicks a user from all free channels by banning and immediately unbanning."""
    if is_admin(user_id): return
    logger.info(f"Kicking user {user_id} from all free channels.")
    for channel_id in FREE_CHANNELS.keys():
        try:
            await context.bot.ban_chat_member(chat_id=channel_id, user_id=user_id)
            await context.bot.unban_chat_member(chat_id=channel_id, user_id=user_id)
            logger.info(f"User {user_id} kicked from channel {channel_id}.")
        except Exception as e:
            logger.error(f"Failed to kick user {user_id} from channel {channel_id}: {e}")


# --- CHAT MEMBER HANDLER ---
async def track_bot_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tracks when the bot is added to or removed from a chat."""
    result = update.my_chat_member
    if not result: return

    chat = result.chat
    new_status = result.new_chat_member.status

    if new_status in [ChatMember.MEMBER, ChatMember.ADMINISTRATOR]:
        logger.info(f"Bot was added to chat '{chat.title}' ({chat.id}).")
        ACTIVE_CHATS[chat.id] = chat.title
    elif new_status in [ChatMember.LEFT, ChatMember.BANNED]:
        logger.info(f"Bot was removed from chat '{chat.title}' ({chat.id}).")
        if chat.id in ACTIVE_CHATS:
            ACTIVE_CHATS.pop(chat.id)
    
    save_data()

async def track_user_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    result = update.chat_member
    if not result: return
    user = result.from_user
    if is_admin(user.id): return

    old_status = result.old_chat_member.status
    new_status = result.new_chat_member.status
    was_member = old_status in [ChatMember.OWNER, ChatMember.ADMINISTRATOR, ChatMember.MEMBER]
    is_now_kicked_or_left = new_status in [ChatMember.LEFT, ChatMember.BANNED]

    if was_member and is_now_kicked_or_left:
        if result.chat.id == MANDATORY_CHANNEL_ID:
            logger.info(f"User {user.id} left mandatory channel. Kicking from all free channels.")
            await remove_user_from_free_channels(user.id, context)
        elif result.chat.type == ChatType.PRIVATE:
            logger.info(f"User {user.id} blocked the bot. Kicking from all free channels.")
            user_info = USER_DATA.pop(user.id, {'full_name': user.full_name, 'username': user.username})
            try:
                username = f"@{user_info['username']}" if user_info.get('username') else "N/A"
                log_message = (f"🚫 **User Blocked Bot** 🚫\n\n"
                               f"**Name:** {user_info.get('full_name')}\n"
                               f"**Username:** {username}\n"
                               f"**ID:** `{user.id}`")
                await context.bot.send_message(chat_id=LOG_CHANNEL_ID, text=log_message, parse_mode='Markdown')
            except Exception as e:
                logger.error(f"Failed to send block notification to log channel: {e}")
            await remove_user_from_free_channels(user.id, context)

# --- MENU & BUTTONS ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user: return # FIX: Handle cases where user is None
    if user.id in BLOCKED_USER_IDS: return

    USER_DATA[user.id] = {'full_name': user.full_name, 'username': user.username}
    
    if is_owner(user.id):
        keyboard = [[InlineKeyboardButton("👑 एडमिन पैनल", callback_data='admin_panel')], [InlineKeyboardButton("🔑 मालिक पैनल", callback_data='owner_panel')]]
        await update.message.reply_text("नमस्ते, मालिक! कृपया एक विकल्प चुनें:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif is_admin(user.id):
        keyboard = [[InlineKeyboardButton("👑 एडमिन पैनल", callback_data='admin_panel')]]
        await update.message.reply_text("नमस्ते, एडमिन! कृपया एक विकल्प चुनें:", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        is_member = await is_user_member_of_channel(user.id, context)
        if is_member:
            keyboard = [
                [InlineKeyboardButton("🆓 फ्री चैनल", callback_data='show_free_channels'), InlineKeyboardButton("💎 पेड चैनल", callback_data='show_paid_channels')],
                [InlineKeyboardButton("📢 अनिवार्य चैनल", url=MANDATORY_CHANNEL_LINK), InlineKeyboardButton("📞 एडमिन से संपर्क करें", url=CONTACT_ADMIN_LINK)],
                [InlineKeyboardButton("🆔 मेरी ID", callback_data='get_my_id')]
            ]
            await update.message.reply_text(f"नमस्ते, {user.first_name}! आपका स्वागत है।", reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            if user.id in USER_DATA:
                await remove_user_from_free_channels(user.id, context)
            welcome_message = (f"<b>WELCOME TO H4R BATCH BOT</b>\n\nनमस्ते, {user.first_name}!\n\n"
                               "<b>चेतावनी:</b> यदि आप इस बॉट को ब्लॉक करते हैं या मुख्य चैनल को छोड़ देते हैं, तो आपको सभी फ्री चैनलों से हटा दिया जाएगा।\n\n"
                               "बॉट का उपयोग करने के लिए कृपया चैनल ज्वाइन करें और फिर 'मैंने ज्वाइन कर लिया है' बटन दबाएं।")
            keyboard = [[InlineKeyboardButton("➡️ चैनल ज्वाइन करें", url=MANDATORY_CHANNEL_LINK)], [InlineKeyboardButton("✅ मैंने ज्वाइन कर लिया है", callback_data='verify_join')]]
            await update.message.reply_html(welcome_message, reply_markup=InlineKeyboardMarkup(keyboard))

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id in BLOCKED_USER_IDS: return
    
    if is_admin(user.id):
        help_text = "नमस्ते! सभी प्रबंधन विकल्पों के लिए कृपया /start कमांड का उपयोग करके बटन वाला मेनू खोलें।"
    else:
        is_member = await is_user_member_of_channel(user.id, context)
        if is_member:
            help_text = "नमस्ते! आप हमारे सदस्य हैं। चैनलों की सूची देखने के लिए /start कमांड का उपयोग करके मेनू खोल सकते हैं।"
        else:
            help_text = "नमस्ते! इस बॉट का उपयोग करने के लिए, कृपया पहले अनिवार्य चैनल ज्वाइन करें। आप /start कमांड से ज्वाइन लिंक प्राप्त कर सकते हैं।"
        
    await update.message.reply_text(help_text)

async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if not user or not chat: return

    if chat.type == ChatType.PRIVATE:
        text = f"Aapki User ID hai: <code>{user.id}</code>\n(Click karke copy karein)"
    else:
        text = f"Is {chat.type.capitalize()} ki Chat ID hai: <code>{chat.id}</code>\n(Click karke copy karein)"
    await update.message.reply_html(text)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    if user_id in BLOCKED_USER_IDS: 
        await query.answer("You are blocked from using this bot.", show_alert=True)
        return
    
    # --- Join Verification ---
    if query.data == 'verify_join':
        await query.answer("जाँच हो रही है...")
        is_member = await is_user_member_of_channel(user_id, context)
        if is_member:
            await query.answer("धन्यवाद! आपका स्वागत है।")
            keyboard = [
                [InlineKeyboardButton("🆓 फ्री चैनल", callback_data='show_free_channels'), InlineKeyboardButton("💎 पेड चैनल", callback_data='show_paid_channels')],
                [InlineKeyboardButton("📢 अनिवार्य चैनल", url=MANDATORY_CHANNEL_LINK), InlineKeyboardButton("📞 एडमिन से संपर्क करें", url=CONTACT_ADMIN_LINK)],
                [InlineKeyboardButton("🆔 मेरी ID", callback_data='get_my_id')]
            ]
            await query.edit_message_text(f"नमस्ते, {query.from_user.first_name}! आपका स्वागत है।", reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await query.answer("आप अभी तक चैनल में शामिल नहीं हुए हैं। कृपया ज्वाइन करें और फिर से प्रयास करें।", show_alert=True)
        return

    # --- Get User ID ---
    if query.data == 'get_my_id':
        await query.answer(f"आपकी यूजर आईडी: {user_id}", show_alert=True)
        return

    # --- Leave Chat Action ---
    if query.data.startswith('leave_chat_'):
        if not is_owner(user_id): return
        chat_id_to_leave = int(query.data.split('_')[-1])
        try:
            await context.bot.leave_chat(chat_id=chat_id_to_leave)
            await query.answer(f"चैट {chat_id_to_leave} को सफलतापूर्वक छोड़ दिया।")
            if chat_id_to_leave in ACTIVE_CHATS:
                ACTIVE_CHATS.pop(chat_id_to_leave)
                save_data()
            # Refresh the list
            keyboard = []
            if ACTIVE_CHATS:
                for chat_id, title in ACTIVE_CHATS.items():
                    keyboard.append([InlineKeyboardButton(f"{title} ({chat_id})", callback_data='noop'), InlineKeyboardButton("❌ Leave", callback_data=f'leave_chat_{chat_id}')])
            keyboard.append([InlineKeyboardButton("⬅️ वापस", callback_data='owner_panel')])
            await query.edit_message_text("बॉट इन ग्रुप/चैनलों में है:", reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception as e:
            await query.answer(f"चैट छोड़ने में विफल: {e}", show_alert=True)
        return

    await query.answer()
    
    # --- Main Menus ---
    if query.data == 'start_member':
        keyboard = [
            [InlineKeyboardButton("🆓 फ्री चैनल", callback_data='show_free_channels'), InlineKeyboardButton("💎 पेड चैनल", callback_data='show_paid_channels')],
            [InlineKeyboardButton("📢 अनिवार्य चैनल", url=MANDATORY_CHANNEL_LINK), InlineKeyboardButton("📞 एडमिन से संपर्क करें", url=CONTACT_ADMIN_LINK)],
            [InlineKeyboardButton("🆔 मेरी ID", callback_data='get_my_id')]
        ]
        await query.edit_message_text(f"नमस्ते, {query.from_user.first_name}! आपका स्वागत है।", reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data == 'main_menu_owner':
        keyboard = [[InlineKeyboardButton("👑 एडमिन पैनल", callback_data='admin_panel')], [InlineKeyboardButton("🔑 मालिक पैनल", callback_data='owner_panel')]]
        await query.edit_message_text("नमस्ते, मालिक! कृपया एक विकल्प चुनें:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data == 'admin_panel':
        if not is_admin(user_id): return
        keyboard = [[InlineKeyboardButton("📢 ब्रॉडकास्ट", callback_data='ask_broadcast_msg'), InlineKeyboardButton("✍️ पोस्ट", callback_data='ask_post_msg')],
                    [InlineKeyboardButton("🆓 फ्री चैनल प्रबंधित करें", callback_data='manage_free_channels')],
                    [InlineKeyboardButton("💎 पेड चैनल प्रबंधित करें", callback_data='manage_paid_channels')]]
        if is_owner(user_id):
            keyboard.append([InlineKeyboardButton("⬅️ वापस", callback_data='main_menu_owner')])
        await query.edit_message_text(text="👑 एडमिन पैनल:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data == 'owner_panel':
        if not is_owner(user_id): return
        keyboard = [[InlineKeyboardButton("➕ एडमिन जोड़ें", callback_data='ask_add_admin'), InlineKeyboardButton("➖ एडमिन हटाएं", callback_data='ask_remove_admin')],
                    [InlineKeyboardButton("📋 एडमिन सूची", callback_data='list_admins')],
                    [InlineKeyboardButton("👥 उपयोगकर्ता प्रबंधित करें", callback_data='manage_users')],
                    [InlineKeyboardButton("📡 Join List", callback_data='join_list')],
                    [InlineKeyboardButton("⬅️ वापस", callback_data='main_menu_owner')]]
        await query.edit_message_text(text="🔑 मालिक पैनल:", reply_markup=InlineKeyboardMarkup(keyboard))

    # --- Ask for Input ---
    elif query.data.startswith('ask_'):
        if not is_admin(user_id): return
        action = query.data.split('_', 1)[1]
        prompts = {
            'broadcast_msg': ("सभी उपयोगकर्ताओं को भेजने के लिए संदेश भेजें:", 'awaiting_broadcast_message', 'admin_panel'),
            'post_msg': ("सभी फ्री चैनलों पर भेजने के लिए संदेश भेजें:", 'awaiting_post_message', 'admin_panel'),
            'add_admin': ("नए एडमिन की यूजर आईडी भेजें:", 'awaiting_add_admin_id', 'owner_panel'),
            'remove_admin': ("हटाने के लिए एडमिन की यूजर आईडी भेजें:", 'awaiting_remove_admin_id', 'owner_panel'),
            'block_user': ("ब्लॉक करने के लिए यूजर की आईडी भेजें:", 'awaiting_block_user_id', 'manage_users'),
            'unblock_user': ("अनब्लॉक करने के लिए यूजर की आईडी भेजें:", 'awaiting_unblock_user_id', 'manage_users'),
            'add_free_channel_name': ("कृपया नए फ्री बैच का नाम भेजें:", 'awaiting_free_channel_name', 'manage_free_channels'),
            'remove_free_channel': ("हटाने के लिए फ्री चैनल का नंबर भेजें:", 'awaiting_remove_free_channel_num', 'manage_free_channels'),
            'add_paid_channel_name': ("कृपया नए पेड बैच का नाम भेजें:", 'awaiting_paid_channel_name', 'manage_paid_channels'),
            'remove_paid_channel': ("हटाने के लिए पेड चैनल का नंबर भेजें:", 'awaiting_remove_paid_channel_num', 'manage_paid_channels'),
        }
        if action in prompts:
            prompt_text, state, back_cb = prompts[action]
            context.user_data['next_step'] = state
            keyboard = [[InlineKeyboardButton("⬅️ वापस", callback_data=back_cb)]]
            await query.edit_message_text(prompt_text, reply_markup=InlineKeyboardMarkup(keyboard))

    # --- Manage Menus ---
    elif query.data == 'manage_free_channels':
        if not is_admin(user_id): return
        keyboard = [[InlineKeyboardButton("➕ जोड़ें", callback_data='ask_add_free_channel_name'), InlineKeyboardButton("➖ हटाएं", callback_data='ask_remove_free_channel')],
                    [InlineKeyboardButton("📋 सूची देखें", callback_data='list_free_channels_admin')],
                    [InlineKeyboardButton("⬅️ वापस", callback_data='admin_panel')]]
        await query.edit_message_text("🆓 फ्री चैनल प्रबंधित करें:", reply_markup=InlineKeyboardMarkup(keyboard))
        
    elif query.data == 'manage_paid_channels':
        if not is_admin(user_id): return
        keyboard = [[InlineKeyboardButton("➕ जोड़ें", callback_data='ask_add_paid_channel_name'), InlineKeyboardButton("➖ हटाएं", callback_data='ask_remove_paid_channel')],
                    [InlineKeyboardButton("📋 सूची देखें", callback_data='list_paid_channels_admin')],
                    [InlineKeyboardButton("⬅️ वापस", callback_data='admin_panel')]]
        await query.edit_message_text("💎 पेड चैनल प्रबंधित करें:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data == 'manage_users':
        if not is_owner(user_id): return
        keyboard = [[InlineKeyboardButton("📋 उपयोगकर्ता सूची", callback_data='list_users'), InlineKeyboardButton("📊 बॉट आँकड़े", callback_data='bot_stats')],
                    [InlineKeyboardButton("🚫 ब्लॉक उपयोगकर्ता", callback_data='ask_block_user'), InlineKeyboardButton("✅ अनब्लॉक उपयोगकर्ता", callback_data='ask_unblock_user')],
                    [InlineKeyboardButton("📜 ब्लॉक सूची", callback_data='list_blocked_users')],
                    [InlineKeyboardButton("⬅️ वापस", callback_data='owner_panel')]]
        await query.edit_message_text("👥 उपयोगकर्ता प्रबंधित करें:", reply_markup=InlineKeyboardMarkup(keyboard))
    
    # --- List Actions ---
    elif query.data == 'list_admins':
        if not is_owner(user_id): return
        admin_list_str = "\n".join(map(str, ADMIN_IDS))
        keyboard = [[InlineKeyboardButton("⬅️ वापस", callback_data='owner_panel')]]
        await query.edit_message_text(f"<b>मालिक:</b> {OWNER_ID}\n\n<b>सभी एडमिन:</b>\n{admin_list_str}", parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data == 'list_users':
        if not is_owner(user_id): return
        keyboard = [[InlineKeyboardButton("⬅️ वापस", callback_data='manage_users')]]
        if USER_DATA:
            user_list = []
            for uid, data in USER_DATA.items():
                if uid in ADMIN_IDS: continue
                username = f"@{data['username']}" if data['username'] else "N/A"
                user_list.append(f"<b>{data['full_name']}</b>\n{username}\n<code>{uid}</code>")
            
            if user_list:
                full_list_str = "\n\n".join(user_list)
                await query.edit_message_text(f"<b>बॉट उपयोगकर्ता:</b>\n\n{full_list_str}", parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                await query.edit_message_text("एडमिन के अलावा कोई उपयोगकर्ता नहीं है।", reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await query.edit_message_text("कोई उपयोगकर्ता नहीं मिला।", reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data == 'list_blocked_users':
        if not is_owner(user_id): return
        keyboard = [[InlineKeyboardButton("⬅️ वापस", callback_data='manage_users')]]
        if BLOCKED_USER_IDS:
            blocked_list_str = "\n".join(f"<code>{uid}</code>" for uid in BLOCKED_USER_IDS)
            await query.edit_message_text(f"<b>ब्लॉक किए गए उपयोगकर्ता:</b>\n\n{blocked_list_str}", parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await query.edit_message_text("कोई उपयोगकर्ता ब्लॉक नहीं है।", reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data == 'bot_stats':
        if not is_owner(user_id): return
        keyboard = [[InlineKeyboardButton("⬅️ वापस", callback_data='manage_users')]]
        total_users = len(USER_DATA)
        admin_count = len(ADMIN_IDS)
        blocked_count = len(BLOCKED_USER_IDS)
        normal_users = total_users - admin_count
        
        stats_text = (
            f"📊 **बॉट आँकड़े** 📊\n\n"
            f"कुल ज्ञात उपयोगकर्ता: {total_users}\n"
            f"एडमिन: {admin_count}\n"
            f"सामान्य उपयोगकर्ता: {normal_users}\n"
            f"ब्लॉक किए गए उपयोगकर्ता: {blocked_count}"
        )
        await query.edit_message_text(stats_text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data == 'join_list':
        if not is_owner(user_id): return
        keyboard = []
        if ACTIVE_CHATS:
            for chat_id, title in ACTIVE_CHATS.items():
                keyboard.append([InlineKeyboardButton(f"{title} ({chat_id})", callback_data='noop'), InlineKeyboardButton("❌ Leave", callback_data=f'leave_chat_{chat_id}')])
        keyboard.append([InlineKeyboardButton("⬅️ वापस", callback_data='owner_panel')])
        await query.edit_message_text("बॉट इन ग्रुप/चैनलों में है:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data == 'list_free_channels_admin':
        if not is_admin(user_id): return
        header = "<b>फ्री चैनल सूची (एडमिन व्यू):</b>\n\n"
        keyboard = [[InlineKeyboardButton("⬅️ वापस", callback_data='manage_free_channels')]]
        if FREE_CHANNELS:
            channel_list = "\n".join(f"{i+1}. <a href='{FREE_CHANNEL_LINKS.get(ch_id, '')}'>{title}</a>" for i, (ch_id, title) in enumerate(FREE_CHANNELS.items()))
        else:
            channel_list = "अभी कोई फ्री चैनल उपलब्ध नहीं है।"
        await query.edit_message_text(header + channel_list, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)

    elif query.data == 'list_paid_channels_admin':
        if not is_admin(user_id): return
        header = "<b>पेड चैनल सूची (एडमिन व्यू):</b>\n\n"
        keyboard = [[InlineKeyboardButton("⬅️ वापस", callback_data='manage_paid_channels')]]
        if PAID_CHANNELS:
            paid_list = "\n".join(f"{i+1}. {entry}" for i, entry in enumerate(PAID_CHANNELS))
        else:
            paid_list = "अभी कोई पेड चैनल उपलब्ध नहीं है।"
        await query.edit_message_text(header + paid_list, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)

    # --- User Channel Lists (Buttons) ---
    elif query.data == 'show_free_channels':
        keyboard = []
        for chat_id, title in FREE_CHANNELS.items():
            keyboard.append([InlineKeyboardButton(f"🆓 {title}", callback_data=f'join_free_{chat_id}')])
        keyboard.append([InlineKeyboardButton("⬅️ वापस", callback_data='start_member')])
        await query.edit_message_text("कृपया एक फ्री चैनल चुनें:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data == 'show_paid_channels':
        keyboard = []
        for i, entry in enumerate(PAID_CHANNELS):
            try:
                name = entry.split('<code>')[1].split('</code>')[0]
                keyboard.append([InlineKeyboardButton(f"💎 {name}", callback_data=f'join_paid_{i}')])
            except IndexError:
                keyboard.append([InlineKeyboardButton(f"💎 पेड चैनल {i+1}", callback_data=f'join_paid_{i}')])
        keyboard.append([InlineKeyboardButton("⬅️ वापस", callback_data='start_member')])
        await query.edit_message_text("कृपया एक पेड चैनल चुनें:", reply_markup=InlineKeyboardMarkup(keyboard))
    
    # --- Join Button Actions ---
    elif query.data.startswith('join_'):
        await query.message.delete()
        
        if query.data.startswith('join_free_'):
            chat_id = int(query.data.split('_')[-1])
            link = FREE_CHANNEL_LINKS.get(chat_id)
            if link:
                await context.bot.send_message(chat_id=user_id, text=f"चैनल ज्वाइन करने के लिए नीचे दिए गए बटन पर क्लिक करें:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ अभी ज्वाइन करें", url=link)]]))
            else:
                await query.answer("इस चैनल के लिए लिंक उपलब्ध नहीं है।", show_alert=True)
        
        elif query.data.startswith('join_paid_'):
            index = int(query.data.split('_')[-1])
            if 0 <= index < len(PAID_CHANNELS):
                html_entry = PAID_CHANNELS[index]
                try:
                    link = html_entry.split("href='")[1].split("'")[0]
                    purchase_info = ("\n\n----------------------------------------\n"
                                     "<b>यदि आप कोर्स खरीदने में रुचि रखते हैं, तो कृपया अधिक जानकारी के लिए @H4R_Contact_bot पर संदेश भेजें।</b>")
                    await context.bot.send_message(chat_id=user_id, text=f"चैनल ज्वाइन करने के लिए नीचे दिए गए बटन पर क्लिक करें:{purchase_info}", 
                                                 reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ अभी ज्वाइन करें", url=link)]]),
                                                 parse_mode='HTML',
                                                 disable_web_page_preview=True)
                except IndexError:
                    await query.answer("इस चैनल के लिए लिंक नहीं मिला।", show_alert=True)
        return


# --- INPUT HANDLERS ---
async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id) or 'next_step' not in context.user_data:
        return

    state = context.user_data.pop('next_step')
    text = update.message.text

    # --- Broadcast & Post ---
    if state == 'awaiting_broadcast_message':
        active_users = [uid for uid in USER_DATA.keys() if uid not in ADMIN_IDS and uid not in BLOCKED_USER_IDS]
        await update.message.reply_text(f"{len(active_users)} उपयोगकर्ताओं को संदेश भेजा जा रहा है...")
        success_count, failed_count = 0, 0
        for u_id in active_users:
            try:
                await context.bot.send_message(chat_id=u_id, text=text)
                success_count += 1
            except Exception:
                failed_count += 1
        await update.message.reply_text(f"ब्रॉडकास्ट पूरा हुआ।\nसफलतापूर्वक: {success_count}, विफल: {failed_count}")

    elif state == 'awaiting_post_message':
        successful_posts, failed_posts = 0, []
        for channel_id in FREE_CHANNELS.keys():
            try:
                await context.bot.send_message(chat_id=channel_id, text=text)
                successful_posts += 1
            except Exception as e:
                failed_posts.append(str(channel_id))
        report = f"संदेश सफलतापूर्वक {successful_posts} चैनलों पर भेज दिया गया है।"
        if failed_posts: report += f"\nइन पर भेजने में विफल रहा: {', '.join(failed_posts)}"
        await update.message.reply_text(report)

    # --- Admin & User Management ---
    elif state in ['awaiting_add_admin_id', 'awaiting_remove_admin_id', 'awaiting_block_user_id', 'awaiting_unblock_user_id']:
        if not is_owner(user.id): return
        try:
            target_id = int(text)
            if state == 'awaiting_add_admin_id':
                if target_id not in ADMIN_IDS:
                    ADMIN_IDS.append(target_id)
                    await update.message.reply_text(f"एडमिन {target_id} को सफलतापूर्वक जोड़ दिया गया है।")
                else:
                    await update.message.reply_text("यह यूजर पहले से ही एडमिन है।")
            elif state == 'awaiting_remove_admin_id':
                if target_id == OWNER_ID:
                    await update.message.reply_text("आप मालिक को नहीं हटा सकते।")
                elif target_id in ADMIN_IDS:
                    ADMIN_IDS.remove(target_id)
                    await update.message.reply_text(f"एडमिन {target_id} को सफलतापूर्वक हटा दिया गया है।")
                else:
                    await update.message.reply_text("यह यूजर एडमिन नहीं है।")
            elif state == 'awaiting_block_user_id':
                if target_id == OWNER_ID or target_id in ADMIN_IDS:
                    await update.message.reply_text("आप किसी एडमिन या मालिक को ब्लॉक नहीं कर सकते।")
                else:
                    BLOCKED_USER_IDS.add(target_id)
                    await update.message.reply_text(f"उपयोगकर्ता {target_id} को सफलतापूर्वक ब्लॉक कर दिया गया है।")
            elif state == 'awaiting_unblock_user_id':
                if target_id in BLOCKED_USER_IDS:
                    BLOCKED_USER_IDS.remove(target_id)
                    await update.message.reply_text(f"उपयोगकर्ता {target_id} को सफलतापूर्वक अनब्लॉक कर दिया गया है।")
                else:
                    await update.message.reply_text("यह उपयोगकर्ता ब्लॉक सूची में नहीं है।")
            save_data()
        except ValueError:
            await update.message.reply_text("अमान्य यूजर आईडी।")

    # --- Channel Management ---
    elif state == 'awaiting_free_channel_name':
        context.user_data['new_channel_name'] = text
        context.user_data['next_step'] = 'awaiting_free_channel_link'
        await update.message.reply_text("ठीक है, नाम सेट हो गया।\n\nअब इस बैच का इनवाइट लिंक भेजें (https://t.me/+...):")
    
    elif state == 'awaiting_free_channel_link':
        context.user_data['new_channel_link'] = text
        context.user_data['next_step'] = 'awaiting_free_channel_chat_id'
        await update.message.reply_text("ठीक है, लिंक सेट हो गया।\n\nअब इस बैच की चैट आईडी भेजें (-100...):")

    elif state == 'awaiting_free_channel_chat_id':
        name = context.user_data.pop('new_channel_name', None)
        link = context.user_data.pop('new_channel_link', None)
        try:
            chat_id = int(text)
            if not str(chat_id).startswith("-100"):
                await update.message.reply_text("अमान्य चैट आईडी। यह -100 से शुरू होनी चाहिए। कृपया फिर से प्रयास करें।")
                return

            if name and link:
                FREE_CHANNELS[chat_id] = name
                FREE_CHANNEL_LINKS[chat_id] = link
                save_data()
                await update.message.reply_text(f"सफलता! फ्री चैनल '{name}' को सूची में जोड़ दिया गया है।")
            else:
                await update.message.reply_text("कुछ जानकारी गुम थी। कृपया प्रक्रिया फिर से शुरू करें।")
        except ValueError:
            await update.message.reply_text("अमान्य चैट आईडी। कृपया केवल नंबर भेजें।")

    elif state == 'awaiting_remove_free_channel_num':
        try:
            index_to_remove = int(text) - 1
            channel_ids = list(FREE_CHANNELS.keys())
            if 0 <= index_to_remove < len(channel_ids):
                removed_channel_id = channel_ids[index_to_remove]
                removed_channel_title = FREE_CHANNELS.pop(removed_channel_id)
                FREE_CHANNEL_LINKS.pop(removed_channel_id, None)
                save_data()
                await update.message.reply_text(f"फ्री चैनल '{removed_channel_title}' को सूची से हटा दिया गया है।")
            else:
                await update.message.reply_text("अमान्य नंबर।")
        except ValueError:
            await update.message.reply_text("कृपया एक नंबर भेजें।")

    elif state == 'awaiting_paid_channel_name':
        context.user_data['new_channel_name'] = text
        context.user_data['next_step'] = 'awaiting_paid_channel_link'
        await update.message.reply_text("अब इस बैच का इनवाइट लिंक भेजें (https://t.me/+...):")

    elif state == 'awaiting_paid_channel_link':
        name = context.user_data.pop('new_channel_name', 'N/A')
        link = text
        html_entry = f"<a href='{link}'>💎<code>{name}</code></a> - प्रीमियम कंटेंट के लिए।"
        PAID_CHANNELS.append(html_entry)
        save_data()
        await update.message.reply_text(f"पेड चैनल '{name}' सफलतापूर्वक जोड़ दिया गया है।")

    elif state == 'awaiting_remove_paid_channel_num':
        try:
            index_to_remove = int(text) - 1
            if 0 <= index_to_remove < len(PAID_CHANNELS):
                removed_entry = PAID_CHANNELS.pop(index_to_remove)
                save_data()
                await update.message.reply_html(f"पेड चैनल एंट्री हटा दी गई: {removed_entry}")
            else:
                await update.message.reply_text("अमान्य नंबर।")
        except ValueError:
            await update.message.reply_text("कृपया एक नंबर भेजें।")

# --- Global Error Handler ---
async def error_handler(update, context):
    try:
        context.application.logger.exception("Unhandled exception while handling update: %s", update)
    except Exception:
        pass



def main():
    """Starts the bot."""
    load_data() # Load data on startup
    
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Register handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("id", id_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_error_handler(error_handler)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))
    application.add_handler(ChatMemberHandler(track_user_status, ChatMemberHandler.CHAT_MEMBER))
    application.add_handler(ChatMemberHandler(track_bot_status, ChatMemberHandler.MY_CHAT_MEMBER))
    
    print("बॉट शुरू हो गया है... (उपयोगकर्ता प्रबंधन के साथ)")
    application.run_polling()


if __name__ == '__main__':
    main()
