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
# --- Get information from Render Environment Variables ---

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

# --- PERMISSION CHEKS ---
def is_owner(user_id: int) -> bool: return user_id == OWNER_ID
def is_admin(user_id: int) -> bool: return user_id in ADMIN_IDS


# --- HELPER FUNCTIONS ---
async def job_delete_message(context: ContextTypes.DEFAULT_TYPE):
    """Callback function for the job queue to delete a message."""
    job = context.job
    try:
        await context.bot.delete_message(chat_id=job.chat_id, message_id=job.data)
        logger.info(f"Job: Deleted message {job.data} in chat {job.chat_id}")
    except Exception as e:
        # Message might have been deleted by the user already, so we can ignore it.
        logger.warning(f"Job: Failed to delete message {job.data} in chat {job.chat_id}: {e}")

async def is_user_member_of_channel(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if is_admin(user_id): return True
    try:
        member = await context.bot.get_chat_member(chat_id=MANDATORY_CHANNEL_ID, user_id=user_id)
        return member.status in [ChatMember.OWNER, ChatMember.ADMINISTRATOR, ChatMember.MEMBER]
    except Exception as e:
        logger.error(f"Error checking membership for {user_id} in {MANDATORY_CHANNEL_ID}: {e}")
        return False

async def remove_user_from_free_channels(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Kicks a user from all free batches by banning and immediately unbanning."""
    if is_admin(user_id): return
    logger.info(f"Kicking user {user_id} from all free batches.")
    for channel_id in FREE_CHANNELS.keys():
        try:
            await context.bot.ban_chat_member(chat_id=channel_id, user_id=user_id)
            await context.bot.unban_chat_member(chat_id=channel_id, user_id=user_id)
            logger.info(f"User {user_id} kicked from batch {channel_id}.")
        except Exception as e:
            logger.error(f"Failed to kick user {user_id} from batch {channel_id}: {e}")


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
            logger.info(f"User {user.id} left mandatory channel. Kicking from all free batches.")
            await remove_user_from_free_channels(user.id, context)
        elif result.chat.type == ChatType.PRIVATE:
            logger.info(f"User {user.id} blocked the bot. Kicking from all free batches.")
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
        keyboard = [[InlineKeyboardButton("👑 Admin Panel", callback_data='admin_panel')], [InlineKeyboardButton("🔑 Owner Panel", callback_data='owner_panel')]]
        await update.message.reply_text("Hello, Owner! Please choose an option:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif is_admin(user.id):
        keyboard = [[InlineKeyboardButton("👑 Admin Panel", callback_data='admin_panel')]]
        await update.message.reply_text("Hello, Admin! Please choose an option:", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        is_member = await is_user_member_of_channel(user.id, context)
        if is_member:
            keyboard = [
                [InlineKeyboardButton("🆓 Free Batches", callback_data='show_free_channels'), InlineKeyboardButton("💎 Paid Channels", callback_data='show_paid_channels')],
                [InlineKeyboardButton("📢 Mandatory Channel", url=MANDATORY_CHANNEL_LINK), InlineKeyboardButton("📞 Contact Admin", url=CONTACT_ADMIN_LINK)],
                [InlineKeyboardButton("🆔 My ID", callback_data='get_my_id')]
            ]
            await update.message.reply_text(f"Hello, {user.first_name}! Welcome.", reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            if user.id in USER_DATA:
                await remove_user_from_free_channels(user.id, context)
            welcome_message = (f"<b>WELCOME TO H4R BATCH BOT</b>\n\nHello, {user.first_name}!\n\n"
                               "<b>Warning:</b> If you block this bot or leave the main channel, you will be removed from all free batches.\n\n"
                               "To use the bot, please join the channel and then press the 'I have joined' button.")
            keyboard = [[InlineKeyboardButton("➡️ Join Channel", url=MANDATORY_CHANNEL_LINK)], [InlineKeyboardButton("✅ I have joined", callback_data='verify_join')]]
            await update.message.reply_html(welcome_message, reply_markup=InlineKeyboardMarkup(keyboard))

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id in BLOCKED_USER_IDS: return
    
    if is_admin(user.id):
        help_text = "Hello! For all management options, please open the button menu using the /start command."
    else:
        is_member = await is_user_member_of_channel(user.id, context)
        if is_member:
            help_text = "Hello! You are a member. You can open the menu using the /start command to see the list of channels."
        else:
            help_text = "Hello! To use this bot, please join the mandatory channel first. You can get the join link with the /start command."
        
    await update.message.reply_text(help_text)

async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /id command in private chats, groups, and channels."""
    message = update.effective_message
    if not message:
        logger.warning("id_command could not find a message in the update.")
        return

    chat = message.chat
    
    if chat.type == ChatType.PRIVATE:
        text = f"Your User ID is: <code>{message.from_user.id}</code>\n(Click to copy)"
    elif chat.type == ChatType.CHANNEL:
        text = f"This Channel's Chat ID is: <code>{chat.id}</code>\n(Click to copy)"
    else: # Groups and Supergroups
        text = f"This {chat.type.capitalize()}'s Chat ID is: <code>{chat.id}</code>\n(Click to copy)"
    
    try:
        await message.reply_html(text)
    except Exception as e:
        logger.error(f"Failed to send /id reply in chat {chat.id}: {e}")
        
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    if user_id in BLOCKED_USER_IDS: 
        await query.answer("You are blocked from using this bot.", show_alert=True)
        return
    
    # --- Join Verification ---
    if query.data == 'verify_join':
        await query.answer("Verifying...")
        is_member = await is_user_member_of_channel(user_id, context)
        if is_member:
            await query.answer("Thank you! Welcome.")
            keyboard = [
                [InlineKeyboardButton("🆓 Free Batches", callback_data='show_free_channels'), InlineKeyboardButton("💎 Paid Channels", callback_data='show_paid_channels')],
                [InlineKeyboardButton("📢 Mandatory Channel", url=MANDATORY_CHANNEL_LINK), InlineKeyboardButton("📞 Contact Admin", url=CONTACT_ADMIN_LINK)],
                [InlineKeyboardButton("🆔 My ID", callback_data='get_my_id')]
            ]
            await query.edit_message_text(f"Hello, {query.from_user.first_name}! Welcome.", reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await query.answer("You have not joined the channel yet. Please join and try again.", show_alert=True)
        return

    # --- Get User ID ---
    if query.data == 'get_my_id':
        await query.answer(f"Your User ID: {user_id}", show_alert=True)
        return

    # --- Leave Chat Action ---
    if query.data.startswith('leave_chat_'):
        if not is_owner(user_id): return
        chat_id_to_leave = int(query.data.split('_')[-1])
        try:
            await context.bot.leave_chat(chat_id=chat_id_to_leave)
            await query.answer(f"Successfully left chat {chat_id_to_leave}.")
            if chat_id_to_leave in ACTIVE_CHATS:
                ACTIVE_CHATS.pop(chat_id_to_leave)
                save_data()
            # Refresh the list
            keyboard = []
            if ACTIVE_CHATS:
                for chat_id, title in ACTIVE_CHATS.items():
                    keyboard.append([InlineKeyboardButton(f"{title} ({chat_id})", callback_data='noop'), InlineKeyboardButton("❌ Leave", callback_data=f'leave_chat_{chat_id}')])
            keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data='owner_panel')])
            await query.edit_message_text("The bot is in these groups/channels:", reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception as e:
            await query.answer(f"Failed to leave chat: {e}", show_alert=True)
        return

    await query.answer()
    
    # --- Main Menus ---
    if query.data == 'start_member':
        keyboard = [
            [InlineKeyboardButton("🆓 Free Batches", callback_data='show_free_channels'), InlineKeyboardButton("💎 Paid Channels", callback_data='show_paid_channels')],
            [InlineKeyboardButton("📢 Mandatory Channel", url=MANDATORY_CHANNEL_LINK), InlineKeyboardButton("📞 Contact Admin", url=CONTACT_ADMIN_LINK)],
            [InlineKeyboardButton("🆔 My ID", callback_data='get_my_id')]
        ]
        await query.edit_message_text(f"Hello, {query.from_user.first_name}! Welcome.", reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data == 'main_menu_owner':
        keyboard = [[InlineKeyboardButton("👑 Admin Panel", callback_data='admin_panel')], [InlineKeyboardButton("🔑 Owner Panel", callback_data='owner_panel')]]
        await query.edit_message_text("Hello, Owner! Please choose an option:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data == 'admin_panel':
        if not is_admin(user_id): return
        keyboard = [[InlineKeyboardButton("📢 Broadcast", callback_data='ask_broadcast_msg'), InlineKeyboardButton("✍️ Post", callback_data='ask_post_msg')],
                    [InlineKeyboardButton("🆓 Manage Free Batches", callback_data='manage_free_channels')],
                    [InlineKeyboardButton("💎 Manage Paid Channels", callback_data='manage_paid_channels')]]
        if is_owner(user_id):
            keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data='main_menu_owner')])
        await query.edit_message_text(text="👑 Admin Panel:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data == 'owner_panel':
        if not is_owner(user_id): return
        keyboard = [[InlineKeyboardButton("➕ Add Admin", callback_data='ask_add_admin'), InlineKeyboardButton("➖ Remove Admin", callback_data='ask_remove_admin')],
                    [InlineKeyboardButton("📋 List Admins", callback_data='list_admins')],
                    [InlineKeyboardButton("👥 Manage Users", callback_data='manage_users')],
                    [InlineKeyboardButton("📡 Join List", callback_data='join_list')],
                    [InlineKeyboardButton("⬅️ Back", callback_data='main_menu_owner')]]
        await query.edit_message_text(text="🔑 Owner Panel:", reply_markup=InlineKeyboardMarkup(keyboard))

    # --- Ask for Input ---
    elif query.data.startswith('ask_'):
        if not is_admin(user_id): return
        action = query.data.split('_', 1)[1]
        prompts = {
            'broadcast_msg': ("Send the message to be broadcast to all users:", 'awaiting_broadcast_message', 'admin_panel'),
            'post_msg': ("Send the message to be posted in all free batches:", 'awaiting_post_message', 'admin_panel'),
            'add_admin': ("Send the User ID of the new admin:", 'awaiting_add_admin_id', 'owner_panel'),
            'remove_admin': ("Send the User ID of the admin to remove:", 'awaiting_remove_admin_id', 'owner_panel'),
            'block_user': ("Send the User ID of the user to block:", 'awaiting_block_user_id', 'manage_users'),
            'unblock_user': ("Send the User ID of the user to unblock:", 'awaiting_unblock_user_id', 'manage_users'),
            'add_free_channel_name': ("Please send the name of the new free batch:", 'awaiting_free_channel_name', 'manage_free_channels'),
            'remove_free_channel': ("Send the number of the free batch to remove:", 'awaiting_remove_free_channel_num', 'manage_free_channels'),
            'add_paid_channel_name': ("Please send the name of the new paid batch:", 'awaiting_paid_channel_name', 'manage_paid_channels'),
            'remove_paid_channel': ("Send the number of the paid channel to remove:", 'awaiting_remove_paid_channel_num', 'manage_paid_channels'),
        }
        if action in prompts:
            prompt_text, state, back_cb = prompts[action]
            context.user_data['next_step'] = state
            keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data=back_cb)]]
            await query.edit_message_text(prompt_text, reply_markup=InlineKeyboardMarkup(keyboard))

    # --- Manage Menus ---
    elif query.data == 'manage_free_channels':
        if not is_admin(user_id): return
        keyboard = [[InlineKeyboardButton("➕ Add", callback_data='ask_add_free_channel_name'), InlineKeyboardButton("➖ Remove", callback_data='ask_remove_free_channel')],
                    [InlineKeyboardButton("📋 View List", callback_data='list_free_channels_admin')],
                    [InlineKeyboardButton("⬅️ Back", callback_data='admin_panel')]]
        await query.edit_message_text("🆓 Manage Free Batches:", reply_markup=InlineKeyboardMarkup(keyboard))
        
    elif query.data == 'manage_paid_channels':
        if not is_admin(user_id): return
        keyboard = [[InlineKeyboardButton("➕ Add", callback_data='ask_add_paid_channel_name'), InlineKeyboardButton("➖ Remove", callback_data='ask_remove_paid_channel')],
                    [InlineKeyboardButton("📋 View List", callback_data='list_paid_channels_admin')],
                    [InlineKeyboardButton("⬅️ Back", callback_data='admin_panel')]]
        await query.edit_message_text("💎 Manage Paid Channels:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data == 'manage_users':
        if not is_owner(user_id): return
        keyboard = [[InlineKeyboardButton("📋 User List", callback_data='list_users'), InlineKeyboardButton("📊 Bot Stats", callback_data='bot_stats')],
                    [InlineKeyboardButton("🚫 Block User", callback_data='ask_block_user'), InlineKeyboardButton("✅ Unblock User", callback_data='ask_unblock_user')],
                    [InlineKeyboardButton("📜 Block List", callback_data='list_blocked_users')],
                    [InlineKeyboardButton("⬅️ Back", callback_data='owner_panel')]]
        await query.edit_message_text("👥 Manage Users:", reply_markup=InlineKeyboardMarkup(keyboard))
    
    # --- List Actions ---
    elif query.data == 'list_admins':
        if not is_owner(user_id): return
        admin_list_str = "\n".join(map(str, ADMIN_IDS))
        keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data='owner_panel')]]
        await query.edit_message_text(f"<b>Owner:</b> {OWNER_ID}\n\n<b>All Admins:</b>\n{admin_list_str}", parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data == 'list_users':
        if not is_owner(user_id): return
        keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data='manage_users')]]
        if USER_DATA:
            user_list = []
            for uid, data in USER_DATA.items():
                if uid in ADMIN_IDS: continue
                username = f"@{data['username']}" if data['username'] else "N/A"
                user_list.append(f"<b>{data['full_name']}</b>\n{username}\n<code>{uid}</code>")
            
            if user_list:
                full_list_str = "\n\n".join(user_list)
                await query.edit_message_text(f"<b>Bot Users:</b>\n\n{full_list_str}", parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                await query.edit_message_text("There are no users other than admins.", reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await query.edit_message_text("No users found.", reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data == 'list_blocked_users':
        if not is_owner(user_id): return
        keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data='manage_users')]]
        if BLOCKED_USER_IDS:
            blocked_list_str = "\n".join(f"<code>{uid}</code>" for uid in BLOCKED_USER_IDS)
            await query.edit_message_text(f"<b>Blocked Users:</b>\n\n{blocked_list_str}", parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await query.edit_message_text("No users are blocked.", reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data == 'bot_stats':
        if not is_owner(user_id): return
        keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data='manage_users')]]
        total_users = len(USER_DATA)
        admin_count = len(ADMIN_IDS)
        blocked_count = len(BLOCKED_USER_IDS)
        normal_users = total_users - admin_count
        
        stats_text = (
            f"📊 **Bot Statistics** 📊\n\n"
            f"Total known users: {total_users}\n"
            f"Admins: {admin_count}\n"
            f"Normal users: {normal_users}\n"
            f"Blocked users: {blocked_count}"
        )
        await query.edit_message_text(stats_text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data == 'join_list':
        if not is_owner(user_id): return
        keyboard = []
        if ACTIVE_CHATS:
            for chat_id, title in ACTIVE_CHATS.items():
                keyboard.append([InlineKeyboardButton(f"{title} ({chat_id})", callback_data='noop'), InlineKeyboardButton("❌ Leave", callback_data=f'leave_chat_{chat_id}')])
        keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data='owner_panel')])
        await query.edit_message_text("The bot is in these groups/channels:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data == 'list_free_channels_admin':
        if not is_admin(user_id): return
        header = "<b>Free Batches List (Admin View):</b>\n\n"
        keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data='manage_free_channels')]]
        if FREE_CHANNELS:
            channel_list = "\n".join(f"{i+1}. <a href='{FREE_CHANNEL_LINKS.get(ch_id, '')}'>{title}</a> - <code>{ch_id}</code>" for i, (ch_id, title) in enumerate(FREE_CHANNELS.items()))
        else:
            channel_list = "No free batches are available at the moment."
        await query.edit_message_text(header + channel_list, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)

    elif query.data == 'list_paid_channels_admin':
        if not is_admin(user_id): return
        header = "<b>Paid Channels List (Admin View):</b>\n\n"
        keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data='manage_paid_channels')]]
        if PAID_CHANNELS:
            paid_list = "\n".join(f"{i+1}. {entry}" for i, entry in enumerate(PAID_CHANNELS))
        else:
            paid_list = "No paid channels are available at the moment."
        await query.edit_message_text(header + paid_list, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)

    # --- User Channel Lists (Buttons) ---
    elif query.data == 'show_free_channels':
        keyboard = []
        for chat_id, title in FREE_CHANNELS.items():
            keyboard.append([InlineKeyboardButton(f"🆓 {title}", callback_data=f'join_free_{chat_id}')])
        keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data='start_member')])
        await query.edit_message_text("Please select a free batch:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data == 'show_paid_channels':
        keyboard = []
        for i, entry in enumerate(PAID_CHANNELS):
            try:
                name = entry.split('<code>')[1].split('</code>')[0]
                keyboard.append([InlineKeyboardButton(f"💎 {name}", callback_data=f'join_paid_{i}')])
            except IndexError:
                keyboard.append([InlineKeyboardButton(f"💎 Paid Channel {i+1}", callback_data=f'join_paid_{i}')])
        keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data='start_member')])
        await query.edit_message_text("Please select a paid channel:", reply_markup=InlineKeyboardMarkup(keyboard))
    
    # --- Join Button Actions ---
    elif query.data.startswith('join_'):
        await query.message.delete()
        
        if query.data.startswith('join_free_'):
            chat_id = int(query.data.split('_')[-1])
            link = FREE_CHANNEL_LINKS.get(chat_id)
            if link:
                sent_message = await context.bot.send_message(
                    chat_id=user_id, 
                    text="Click the button to join the batch.\n\nThis message will disappear in 60 seconds.", 
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Join Now", url=link)]])
                )
                # Schedule the message for deletion
                context.job_queue.run_once(
                    job_delete_message, 
                    when=60, 
                    data=sent_message.message_id, 
                    chat_id=user_id
                )
            else:
                await query.answer("The link for this batch is not available.", show_alert=True)
        
        elif query.data.startswith('join_paid_'):
            index = int(query.data.split('_')[-1])
            if 0 <= index < len(PAID_CHANNELS):
                html_entry = PAID_CHANNELS[index]
                try:
                    link = html_entry.split("href='")[1].split("'")[0]
                    purchase_info = ("\n\n----------------------------------------\n"
                                     "<b>If you are interested in purchasing the course, please message @H4R_Contact_bot for more information.</b>\n\n"
                                     "This message will disappear in 60 seconds.")
                    sent_message = await context.bot.send_message(
                        chat_id=user_id, 
                        text=f"Click the button below to join the channel:{purchase_info}", 
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Join Now", url=link)]]),
                        parse_mode='HTML',
                        disable_web_page_preview=True
                    )
                    # Schedule the message for deletion
                    context.job_queue.run_once(
                        job_delete_message, 
                        when=60, 
                        data=sent_message.message_id, 
                        chat_id=user_id
                    )
                except IndexError:
                    await query.answer("Could not find a link for this channel.", show_alert=True)
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
        await update.message.reply_text(f"Sending message to {len(active_users)} users...")
        success_count, failed_count = 0, 0
        for u_id in active_users:
            try:
                await context.bot.send_message(chat_id=u_id, text=text)
                success_count += 1
            except Exception:
                failed_count += 1
        await update.message.reply_text(f"Broadcast complete.\nSuccess: {success_count}, Failed: {failed_count}")

    elif state == 'awaiting_post_message':
        successful_posts, failed_posts = 0, []
        for channel_id in FREE_CHANNELS.keys():
            try:
                await context.bot.send_message(chat_id=channel_id, text=text)
                successful_posts += 1
            except Exception as e:
                failed_posts.append(str(channel_id))
        report = f"Message successfully sent to {successful_posts} batches."
        if failed_posts: report += f"\nFailed to send to: {', '.join(failed_posts)}"
        await update.message.reply_text(report)

    # --- Admin & User Management ---
    elif state in ['awaiting_add_admin_id', 'awaiting_remove_admin_id', 'awaiting_block_user_id', 'awaiting_unblock_user_id']:
        if not is_owner(user.id): return
        try:
            target_id = int(text)
            if state == 'awaiting_add_admin_id':
                if target_id not in ADMIN_IDS:
                    ADMIN_IDS.append(target_id)
                    await update.message.reply_text(f"Admin {target_id} has been added successfully.")
                else:
                    await update.message.reply_text("This user is already an admin.")
            elif state == 'awaiting_remove_admin_id':
                if target_id == OWNER_ID:
                    await update.message.reply_text("You cannot remove the owner.")
                elif target_id in ADMIN_IDS:
                    ADMIN_IDS.remove(target_id)
                    await update.message.reply_text(f"Admin {target_id} has been removed successfully.")
                else:
                    await update.message.reply_text("This user is not an admin.")
            elif state == 'awaiting_block_user_id':
                if target_id == OWNER_ID or target_id in ADMIN_IDS:
                    await update.message.reply_text("You cannot block an admin or the owner.")
                else:
                    BLOCKED_USER_IDS.add(target_id)
                    await update.message.reply_text(f"User {target_id} has been blocked successfully.")
            elif state == 'awaiting_unblock_user_id':
                if target_id in BLOCKED_USER_IDS:
                    BLOCKED_USER_IDS.remove(target_id)
                    await update.message.reply_text(f"User {target_id} has been unblocked successfully.")
                else:
                    await update.message.reply_text("This user is not in the block list.")
            save_data()
        except ValueError:
            await update.message.reply_text("Invalid User ID.")

    # --- Channel Management ---
    elif state == 'awaiting_free_channel_name':
        context.user_data['new_channel_name'] = text
        context.user_data['next_step'] = 'awaiting_free_channel_link'
        await update.message.reply_text("Okay, the name is set.\n\nNow, send the invite link for this batch (https://t.me/+...):")
    
    elif state == 'awaiting_free_channel_link':
        context.user_data['new_channel_link'] = text
        context.user_data['next_step'] = 'awaiting_free_channel_chat_id'
        await update.message.reply_text("Okay, the link is set.\n\nNow, send the Chat ID for this batch (-100...):")

    elif state == 'awaiting_free_channel_chat_id':
        name = context.user_data.pop('new_channel_name', None)
        link = context.user_data.pop('new_channel_link', None)
        try:
            chat_id = int(text)
            if not str(chat_id).startswith("-100"):
                await update.message.reply_text("Invalid Chat ID. It must start with -100. Please try again.")
                return

            if name and link:
                FREE_CHANNELS[chat_id] = name
                FREE_CHANNEL_LINKS[chat_id] = link
                save_data()
                await update.message.reply_text(f"Success! Free batch '{name}' has been added to the list.")
            else:
                await update.message.reply_text("Some information was missing. Please start the process again.")
        except ValueError:
            await update.message.reply_text("Invalid Chat ID. Please send numbers only.")

    elif state == 'awaiting_remove_free_channel_num':
        try:
            index_to_remove = int(text) - 1
            channel_ids = list(FREE_CHANNELS.keys())
            if 0 <= index_to_remove < len(channel_ids):
                removed_channel_id = channel_ids[index_to_remove]
                removed_channel_title = FREE_CHANNELS.pop(removed_channel_id)
                FREE_CHANNEL_LINKS.pop(removed_channel_id, None)
                save_data()
                await update.message.reply_text(f"Free batch '{removed_channel_title}' has been removed from the list.")
            else:
                await update.message.reply_text("Invalid number.")
        except ValueError:
            await update.message.reply_text("Please send a number.")

    elif state == 'awaiting_paid_channel_name':
        context.user_data['new_channel_name'] = text
        context.user_data['next_step'] = 'awaiting_paid_channel_link'
        await update.message.reply_text("Now send the invite link for this batch (https://t.me/+...):")

    elif state == 'awaiting_paid_channel_link':
        name = context.user_data.pop('new_channel_name', 'N/A')
        link = text
        html_entry = f"<a href='{link}'>💎<code>{name}</code></a> - For premium content."
        PAID_CHANNELS.append(html_entry)
        save_data()
        await update.message.reply_text(f"Paid channel '{name}' added successfully.")

    elif state == 'awaiting_remove_paid_channel_num':
        try:
            index_to_remove = int(text) - 1
            if 0 <= index_to_remove < len(PAID_CHANNELS):
                removed_entry = PAID_CHANNELS.pop(index_to_remove)
                save_data()
                await update.message.reply_html(f"Paid channel entry removed: {removed_entry}")
            else:
                await update.message.reply_text("Invalid number.")
        except ValueError:
            await update.message.reply_text("Please send a number.")

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

    # Yeh handler private chats aur groups ke liye काम करेगा
    application.add_handler(CommandHandler("id", id_command))
    # Yeh NAYA handler khas taur par channels mein commands ke liye hai
    application.add_handler(MessageHandler(filters.COMMAND & filters.ChatType.CHANNEL, id_command))

    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_error_handler(error_handler)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))
    application.add_handler(ChatMemberHandler(track_user_status, ChatMemberHandler.CHAT_MEMBER))
    application.add_handler(ChatMemberHandler(track_bot_status, ChatMemberHandler.MY_CHAT_MEMBER))
    
    print("Bot has started... (with user management)")
    application.run_polling()


if __name__ == '__main__':
    main()
