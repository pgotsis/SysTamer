#!/usr/bin/env python3

import psutil
import hashlib
import asyncio
import subprocess
import httpcore
import nest_asyncio
import telegram.error

try:
    from misc import *
except ImportError:
    from misc import *

import mss
from io import BytesIO
from PIL import Image

from pathlib import Path
from typing import NoReturn, Any, Callable, Awaitable
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes, CommandHandler, CallbackQueryHandler

nest_asyncio.apply()

MAX_TELEGRAM_MSG_LEN = 4000  # a bit less than 4096 to be safe

#   --------------------------------------------------------------------------------------------------------------------
#   ....................................................................................................................
#   .............._______.____    ____  _______.___________.    ___      .___  ___.  _______ .______....................
#   ............./       |\   \  /   / /       |           |   /   \     |   \/   | |   ____||   _  \...................
#   ............|   (----` \   \/   / |   (----`---|  |----`  /  ^  \    |  \  /  | |  |__   |  |_)  |..................
#   .............\   \      \_    _/   \   \       |  |      /  /_\  \   |  |\/|  | |   __|  |      /...................
#   ..........----)   |       |  | .----)   |      |  |     /  _____  \  |  |  |  | |  |____ |  |\  \...................
#   .........|_______/        |__| |_______/       |__|    /__/     \__\ |__|  |__| |_______|| _| \._\..................
#   ....................................................................................................................
#   Ⓒ by https://github.com/flashnuke Ⓒ................................................................................
#   --------------------------------------------------------------------------------------------------------------------

# ============= WRAPPERS =============

def require_authentication(func, *_args, **_kwargs):
    async def _impl(self, update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if context.user_data.get("authenticated", False) or not SysTamer.should_authenticate():
            # User is authenticated, proceed with the function
            return await func(self, update, context, *args, **kwargs)
        else:
            # User is not authenticated, prompt for password
            await update.message.reply_text("please login via /login *<password\>*", parse_mode='MarkdownV2')

    return _impl


def log_action(func, *_args, **_kwargs):
    async def _impl(self, update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        # Try to get the message text from either a message or a callback query
        message_text = None
        if update.message and update.message.text:
            message_text = update.message.text
        elif update.callback_query and update.callback_query.data:
            message_text = update.callback_query.data
        else:
            message_text = ""
        parts = message_text.split()
        command_name = parts[0] if parts else ""
        command_args = parts[1:] if len(parts) > 1 else []
        print_cmd(f"user {SysTamer.get_update_username(update)}\t|\tcmd {command_name}" +
                  (f"\t|\targs {','.join(command_args)}" if command_args else ''))
        return await func(self, update, context, *args, **kwargs)
    return _impl


def check_for_permission(func, *_args, **_kwargs):
    async def _impl(self, update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        try:
            return await func(self, update, context, *args, **kwargs)
        except PermissionError:
            if update.effective_message:
                await update.effective_message.reply_text("No permissions for this action, try running as superuser.")
            if update.callback_query:
                await SysTamer.delete_message(update, context)
    return _impl


def require_allowed_user(func):
    async def _impl(self, update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = str(update.effective_user.id) if update.effective_user else None
        username = update.effective_user.username if update.effective_user and update.effective_user.username else "N/A"
        # Try to get the command attempted
        command = ""
        if update.message and update.message.text:
            command = update.message.text
        elif update.callback_query and update.callback_query.data:
            command = update.callback_query.data
        if SysTamer._ALLOWED_USERS and user_id not in SysTamer._ALLOWED_USERS:
            print_cmd(
                f"Unauthorized access attempt by user {user_id} (username: {username}) | command: {command}"
            )
            if update.message:
                await update.message.reply_text("You are not authorized to use this bot.")
            elif update.callback_query:
                await update.callback_query.answer("You are not authorized to use this bot.", show_alert=True)
            return
        return await func(self, update, context, *args, **kwargs)
    return _impl


class SysTamer:
    _BROWSE_IGNORE_PATH = ".browseignore"
    _PASSWORD = str()
    _ALLOWED_USERS = set()

    def __init__(self, json_conf: dict):
        self._bot_token = json_conf.get("bot_token", None)
        if self._bot_token:
            print_info(f"Bot token was set to -> {BOLD}{self._bot_token}{RESET}")
        else:
            raise Exception("Bot token is missing")

        SysTamer._PASSWORD = json_conf.get("password", str())
        if SysTamer._PASSWORD:
            print_info(f"Password set to -> {BOLD}{SysTamer._PASSWORD}{RESET}")
        else:
            print_info("No password was set, running an unauthenticated session...")
        SysTamer._ALLOWED_USERS = set(str(uid) for uid in json_conf.get("allowed_users", []))
        if SysTamer._ALLOWED_USERS:
            print_info(f"Allowed users set to -> {BOLD}{', '.join(map(str, SysTamer._ALLOWED_USERS))}{RESET}")
        else:
            print_info("No allowed users were set, running an unauthenticated session...")

        self._timeout_duration = json_conf.get("timeout_duration", 10)
        self._uploads_dir = os.path.join(os.getcwd(), "uploads")

        self._application: telegram.ext.Application = self._build_app()

        self._browse_path_dict = dict()
        self._ignored_paths = SysTamer.load_ignore_paths()

    # ============= static method helpers =============

    @staticmethod
    def build_navigate_keyboard(all_buttons: List[InlineKeyboardButton]) -> List[List[InlineKeyboardButton]]:
        # separate navigation buttons from path buttons...
        navigation_buttons = all_buttons[-1] if isinstance(all_buttons[-1], list) else [all_buttons[-1]]
        regular_buttons = all_buttons[:-1] if isinstance(all_buttons[-1], list) else all_buttons
        keyboard = [regular_buttons[i:i + 2] for i in range(0, len(regular_buttons), 2)]
        keyboard.append(navigation_buttons)
        return keyboard

    @staticmethod
    async def delete_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id,
                message_id=update.effective_message.message_id
            )
        except telegram.error.BadRequest as e:
            print_error(f"Error deleting message: {e}")

    @staticmethod
    def get_update_username(update: Update) -> str:
        return update.effective_user.username if update.effective_user.username else update.effective_user.id

    @staticmethod
    async def safe_reply(update: Update, text: str, **kwargs):
        if update.message:
            await update.message.reply_text(text, **kwargs)
        elif update.callback_query:
            # Prefer editing the message for callback queries
            try:
                await update.callback_query.edit_message_text(text, **kwargs)
            except telegram.error.BadRequest:
                # If editing fails (e.g., message already edited), send a new message
                await update.callback_query.message.reply_text(text, **kwargs)

    @staticmethod
    def should_authenticate():
        return len(SysTamer._PASSWORD) > 0

    @staticmethod
    def load_ignore_paths() -> set:
        ignored_paths = set()
        try:
            with open(SysTamer._BROWSE_IGNORE_PATH, 'r') as file:
                for line in file:
                    line = line.strip()
                    if line:  # Ignore empty lines
                        ignored_paths.add(line)
                        full_path = str(Path(line).resolve())
                        ignored_paths.add(full_path)
        except FileNotFoundError:
            print_error(f"{SysTamer._BROWSE_IGNORE_PATH} was not loaded")
        print_info(f"Loaded `/browse` ignore paths from -> {BOLD}{SysTamer._BROWSE_IGNORE_PATH}{RESET}")
        return ignored_paths

    @staticmethod
    def split_message(text, max_length=MAX_TELEGRAM_MSG_LEN):
        """Split text into chunks suitable for Telegram messages."""
        lines = text.splitlines(keepends=True)
        chunks = []
        current = ""
        for line in lines:
            if len(current) + len(line) > max_length:
                chunks.append(current)
                current = ""
            current += line
        if current:
            chunks.append(current)
        return chunks

    async def send_long_message(self, update_or_query, text, parse_mode=None):
        """Send long text as multiple messages/chunks."""
        chunks = self.split_message(text)
        for i, chunk in enumerate(chunks):
            msg = f"```\n{chunk}\n```" if parse_mode and "Markdown" in parse_mode else chunk
            # Handle Update object (from command)
            if hasattr(update_or_query, "message") and update_or_query.message:
                await update_or_query.message.reply_text(msg, parse_mode=parse_mode)
            # Handle CallbackQuery object (from inline button)
            elif hasattr(update_or_query, "edit_message_text"):
                if i == 0:
                    await update_or_query.edit_message_text(msg, parse_mode=parse_mode)
                else:
                    if hasattr(update_or_query, "message") and update_or_query.message:
                        await update_or_query.message.reply_text(msg, parse_mode=parse_mode)

    @log_action
    @require_allowed_user
    async def login(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.should_authenticate():
            await update.message.reply_text("Authentication is not required.")
            return

        # Check if the user provided a password with the /login command
        if len(context.args) == 0:
            await update.message.reply_text("Please provide a password, usage: /login *<password\>*",
                                            parse_mode='MarkdownV2')
            return

        user_password = context.args[0]  # Get the provided password

        await self.delete_message(update, context)

        if user_password == SysTamer._PASSWORD:
            context.user_data["authenticated"] = True
            await update.message.reply_text("Password accepted! You are now authenticated.")
        else:
            await update.message.reply_text("Incorrect password, please try again.")

    @log_action
    async def logout(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.should_authenticate():
            await update.message.reply_text("Cannot logout an authenticated session.")
            return
        if context.user_data.get("authenticated", False):
            self.deauthenticate(context)
            await update.message.reply_text("Logged out successfully.")
        else:
            # User is not authenticated
            await update.message.reply_text("Not logged in.")

    @staticmethod
    def deauthenticate(context: ContextTypes.DEFAULT_TYPE):
        context.user_data["authenticated"] = False

    @log_action
    @require_authentication
    @require_allowed_user
    async def send_screenshot(self, update: Update, _context: ContextTypes.DEFAULT_TYPE):
        with mss.mss() as sct:
            screenshot = sct.grab(sct.monitors[0])  # Capture the full screen

            img = Image.frombytes("RGB", (screenshot.width, screenshot.height), screenshot.rgb)

            max_dimension = 4096
            if img.width > max_dimension or img.height > max_dimension:
                img.thumbnail((max_dimension, max_dimension))

            byte_io = BytesIO()
            img.save(byte_io, 'PNG')
            byte_io.seek(0)

            await self.reply_with_timeout(update, update.message.reply_photo, photo=byte_io)

    async def reply_with_timeout(self, update: Update, async_reply_ptr: Callable[..., Awaitable[Any]], *args, **kwargs):
        try:
            await async_reply_ptr(*args, write_timeout=self._timeout_duration,
                                  connect_timeout=self._timeout_duration, read_timeout=self._timeout_duration, **kwargs)
        except telegram.error.TimedOut as _exc:
            await update.message.reply_text(f"Request timed out after {self._timeout_duration} seconds.")
        except telegram.error.NetworkError as exc:
            await update.message.reply_text(f"Network error occurred: {exc}. Please try again later.")

    @require_authentication
    @require_allowed_user
    async def handle_file_upload(self, update: Update, _context: ContextTypes.DEFAULT_TYPE):
        if not os.path.exists(self._uploads_dir):
            os.makedirs(self._uploads_dir)
        file_path = str()

        if update.message.document:
            file = await update.message.document.get_file()
            filename = update.message.document.file_name
            file_path = os.path.join(self._uploads_dir, filename)
            await file.download_to_drive(file_path)
            await update.message.reply_text(f"Document has been uploaded to '{self._uploads_dir}' as '{filename}'.")

        elif update.message.photo:
            photo = await update.message.photo[-1].get_file()  # Get the best quality photo
            filename = f"{photo.file_id}.jpg"
            file_path = os.path.join(self._uploads_dir, filename)
            await photo.download_to_drive(file_path)
            await update.message.reply_text(f"Photo has been uploaded to '{self._uploads_dir}' as '{filename}'.")

        elif update.message.video:
            video = await update.message.video.get_file()
            filename = f"{update.message.video.file_name or video.file_id}.mp4"
            file_path = os.path.join(self._uploads_dir, filename)
            await video.download_to_drive(file_path)
            await update.message.reply_text(
                f"Video has been uploaded to '{self._uploads_dir}' as '{filename}'.")

        elif update.message.audio:
            audio = await update.message.audio.get_file()
            filename = f"{update.message.audio.file_name or audio.file_id}.mp3"
            file_path = os.path.join(self._uploads_dir, filename)
            await audio.download_to_drive(file_path)
            await update.message.reply_text(
                f"Audio file has been uploaded to '{self._uploads_dir}' as '{filename}'.")

        elif update.message.voice:
            voice = await update.message.voice.get_file()
            filename = f"{voice.file_id}.ogg"
            file_path = os.path.join(self._uploads_dir, filename)
            await voice.download_to_drive(file_path)
            await update.message.reply_text(
                f"Voice message has been uploaded to '{self._uploads_dir}' as '{filename}'.")

        elif update.message.video_note:
            video_note = await update.message.video_note.get_file()
            filename = f"{video_note.file_id}.mp4"
            file_path = os.path.join(self._uploads_dir, filename)
            await video_note.download_to_drive(file_path)
            await update.message.reply_text(
                f"Video note has been uploaded to '{self._uploads_dir}' as '{filename}'.")

        else:
            await update.message.reply_text("No file or media was uploaded. Please try again.")

        print_cmd(f"user {SysTamer.get_update_username(update)}\t|\tuploaded {file_path}")

    @log_action
    @require_authentication
    @require_allowed_user
    async def list_uploads(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            if not os.path.exists(self._uploads_dir):
                await update.message.reply_text("Upload directory does not exist.")
                return

            entries = os.listdir(self._uploads_dir)
            if not entries:
                await update.message.reply_text("Upload directory is empty.")
                return

            response_lines = []
            for index, entry in enumerate(entries, start=1):
                # Truncate the filename to 20 characters, including the extension
                name, ext = os.path.splitext(entry)
                max_name_length = 20 - len(ext)  # Calculate max length for the name part
                if len(entry) > 20:
                    if len(name) > max_name_length:
                        name = name[:max_name_length - 3] + '...'  # Truncate and add '...' if needed

                    entry = name + ext  # Reassemble the filename with the extension

                response_lines.append(f"**{index}**. {entry}")

            response_text = "\n".join(response_lines)
            await update.message.reply_text(response_text, parse_mode='MarkdownV2')

        except Exception as e:
            await update.message.reply_text(f"An error occurred: {str(e)}")

    @log_action
    @require_authentication
    @require_allowed_user
    async def system_resource_monitoring(self, update: Update, _context: ContextTypes.DEFAULT_TYPE):
        cpu_usage = psutil.cpu_percent(interval=1)
        memory_info = psutil.virtual_memory()
        disk_usage = psutil.disk_usage('/')

        await update.message.reply_text(generate_machine_stats_msg("MachineStats", cpu_usage, memory_info, disk_usage),
                                        parse_mode="MarkdownV2")

    @log_action
    @require_authentication
    @require_allowed_user
    async def list_processes(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        processes = []
        args_lower = [i.lower() for i in context.args]

        for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent']):
            try:
                proc_info = proc.as_dict(attrs=['pid', 'name', 'cpu_percent', 'memory_percent'])
                if len(context.args) > 0:
                    # filter provided - and proc is not in list (using any() to check for substr)
                    filter_name = any(s in proc_info['name'].lower() for s in args_lower if proc_info['name'])
                    filter_pid = any(s in str(proc_info['pid']) for s in args_lower)
                    if not (filter_name or filter_pid):
                        continue
                processes.append(proc_info)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

        table_chunks = generate_proc_stats_msg(f"Processes:{len(processes)},"
                                               f"Filters:{context.args if context.args else None}", processes)
        for chunk in table_chunks:
            await update.message.reply_text(f"```{chunk}```", parse_mode="MarkdownV2")

    @log_action
    @require_authentication
    @require_allowed_user
    async def kill_process(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        process_ids = [int(i) for i in context.args]
        if not process_ids:
            await update.message.reply_text("No PID provided, usage: /kill *<pid\>*", parse_mode="MarkdownV2")
            return
        for process_id in process_ids:
            try:
                proc = psutil.Process(process_id)
                proc.terminate()
                await update.message.reply_text(f"Process {process_id} ({proc.name()}) terminated.")
            except (psutil.NoSuchProcess, IndexError, ValueError):
                await update.message.reply_text("Invalid process ID or process does not exist.")

    @log_action
    async def start(self, update: Update, _context: ContextTypes.DEFAULT_TYPE):
        welcome_message = TG_BANNER + START_INTRO + generate_cmd_dict_msg("Commands", COMMANDS_DICT)
        await update.message.reply_text(welcome_message, parse_mode='MarkdownV2', disable_web_page_preview=True)

    @log_action
    @require_authentication
    @require_allowed_user
    async def upload_info(self, update: Update, _context: ContextTypes.DEFAULT_TYPE):
        upload_message = (
            f"Simply send a file, and it will be saved to -> {self._uploads_dir}"
        )
        await update.message.reply_text(upload_message)

    def list_files_and_directories(self, path: str):
        entries = os.listdir(path)
        buttons = []
        self._browse_path_dict.clear()

        for entry in entries:
            full_path = os.path.join(path, entry)
            if full_path in self._ignored_paths or str(Path(full_path).resolve()) in self._ignored_paths:
                continue

            entry_hashed = hashlib.md5(full_path.encode()).hexdigest()
            self._browse_path_dict[entry_hashed] = full_path

            if os.path.isdir(full_path):
                buttons.append(InlineKeyboardButton(entry + '/', callback_data=f"cd {entry_hashed}"))
            else:
                buttons.append(InlineKeyboardButton(entry, callback_data=f"file {entry_hashed}"))

        parent_directory = os.path.dirname(path)
        if os.path.isdir(parent_directory):
            parent_hashed = hashlib.md5(parent_directory.encode()).hexdigest()
            self._browse_path_dict[parent_hashed] = parent_directory
            buttons.append([InlineKeyboardButton("⬅️ Back", callback_data=f"cd {parent_hashed}"),
                            InlineKeyboardButton("❌️ Close", callback_data=f"action close")])
        else:
            buttons.append([InlineKeyboardButton("❌️ Close", callback_data=f"action close")])
        return buttons

    @log_action
    @check_for_permission
    @require_authentication
    @require_allowed_user
    async def browse(self, update: Update, _context: ContextTypes.DEFAULT_TYPE):
        path = str(Path.home())
        all_buttons = self.list_files_and_directories(path)
        keyboard = self.build_navigate_keyboard(all_buttons)

        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text('Choose a directory or file:', reply_markup=reply_markup)

    @check_for_permission
    @require_authentication
    @require_allowed_user
    async def handle_navigation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        data = query.data.split(' ', 1)
        command = data[0]  # The command is the first part (e.g., "cd", "file", "action")
        print_cmd(f"user {SysTamer.get_update_username(update)}\t|\t"
                  f"handle_navigation received cmd -> {' '.join(data)}" +
                  ('\t|\t(' + self._browse_path_dict.get(data[1]) + ')'
                   if len(data) >= 1 and data[1] in self._browse_path_dict else ''))

        if command == "cd":  # Handle directory navigation
            hashed_path = data[1]
            path = self._browse_path_dict.get(hashed_path)

            if path and os.path.isdir(path):  # Ensure the path is a valid directory
                all_buttons = self.list_files_and_directories(path)
                keyboard = self.build_navigate_keyboard(all_buttons)
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text(text=f'Navigating to: {path}', reply_markup=reply_markup)
            else:
                await query.edit_message_text(text="The directory is invalid or does not exist.")

        elif command == "file":  # File clicked, show "Download/Delete/Back" options
            selected_file = self._browse_path_dict.get(data[1])
            parent_directory = os.path.dirname(selected_file)
            parent_hashed = hashlib.md5(parent_directory.encode()).hexdigest()

            # Store the parent directory in browse_path_dict
            self._browse_path_dict[parent_hashed] = parent_directory

            if selected_file and os.path.isfile(selected_file):  # Ensure it's a valid file
                context.user_data['selected_file'] = selected_file

                # Display action keypad
                keyboard = [
                    [InlineKeyboardButton("Download", callback_data="action download")],
                    [InlineKeyboardButton("Delete", callback_data="action delete")],
                    [InlineKeyboardButton("⬅️ Back", callback_data=f"cd {parent_hashed}")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text(text=f"Choose an action for {os.path.basename(selected_file)}:",
                                              reply_markup=reply_markup)
            else:
                await query.edit_message_text(text="The file is invalid or does not exist.")
        elif command == "action":  # Handle file actions (download or delete)
            action_type = data[1]  # This will be either 'download' or 'delete'
            selected_file = context.user_data.get('selected_file')

            if action_type == "download":
                if selected_file:
                    try:
                        with open(selected_file, 'rb') as file:
                            await self.reply_with_timeout(update, query.message.reply_document, document=file)

                    except Exception as e:
                        await query.message.reply_text(f"Error: {str(e)}")

            elif action_type == "delete":
                if selected_file:
                    try:
                        os.remove(selected_file)
                        msg = f"File '{selected_file}' has been deleted."
                    except FileNotFoundError:
                        msg = f"File '{selected_file}' not found."
                    except Exception as e:
                        msg = f"Error: {str(e)} when attempting to delete."
                    await query.edit_message_text(text=msg)

            elif action_type == "close":
                await self.delete_message(update, context)
            else:
                await query.edit_message_text(text="Invalid action selected.")

    @log_action
    @require_authentication
    @require_allowed_user
    async def systemctl_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await self.safe_reply(update, "Usage: /systemctl <command> [service/filter]\n"
                                    "Commands: list, enable, disable, status, start, stop, restart")
            return

        cmd = context.args[0].lower()
        arg = context.args[1] if len(context.args) > 1 else ""

        if cmd == "list":
            filter_str = arg
            try:
                result = subprocess.run(
                    ["systemctl", "list-units", "--type=service", "--no-pager", "--no-legend"],
                    capture_output=True, text=True, check=True
                )
                lines = result.stdout.strip().split('\n')
                filtered = [line for line in lines if filter_str.lower() in line.lower()] if filter_str else lines
                if not filtered:
                    await self.safe_reply(update, "No services found.")
                    return
                # Limit output to avoid flooding
                max_lines = 30
                output = "\n".join(filtered[:max_lines])
                if len(filtered) > max_lines:
                    output += f"\n...and {len(filtered)-max_lines} more."
                await self.safe_reply(update, f"```\n{output}\n```", parse_mode="MarkdownV2")
            except Exception as e:
                await self.safe_reply(update, f"Error: {e}")

        elif cmd in {"start", "stop", "restart"}:
            if not arg:
                await self.safe_reply(update, f"Usage: /systemctl {cmd} <service>")
                return
            # Ask for confirmation
            keyboard = [
                [
                    InlineKeyboardButton("✅ Yes", callback_data=f"systemctl_confirm {cmd} {arg}"),
                    InlineKeyboardButton("❌ No", callback_data="systemctl_cancel")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await self.safe_reply(
                update,
                f"Are you sure you want to *{cmd}* service `{arg}`?",
                reply_markup=reply_markup,
                parse_mode="MarkdownV2"
            )

        elif cmd in {"enable", "disable", "status"}:
            if not arg:
                await self.safe_reply(update, f"Usage: /systemctl {cmd} <service>")
                return
            try:
                result = subprocess.run(
                    ["systemctl", cmd, arg],
                    capture_output=True, text=True
                )
                output = result.stdout.strip() or result.stderr.strip()
                if not output:
                    output = f"systemctl {cmd} {arg} completed (no output)."
                await self.send_long_message(update, output, parse_mode="MarkdownV2")
            except Exception as e:
                await self.safe_reply(update, f"Error: {e}")

        else:
            await self.safe_reply(update, "Unknown systemctl command. Allowed: list, enable, disable, status, start, stop, restart")

    @require_allowed_user
    async def handle_systemctl_confirmation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data.split(' ', 2)
        if data[0] == "systemctl_confirm":
            cmd, arg = data[1], data[2]
            try:
                result = subprocess.run(
                    ["systemctl", cmd, arg],
                    capture_output=True, text=True
                )
                output = result.stdout.strip() or result.stderr.strip()
                if not output:
                    output = f"systemctl {cmd} {arg} completed (no output)."
                await self.send_long_message(query, output, parse_mode="MarkdownV2")
            except Exception as e:
                await query.edit_message_text(f"Error: {e}")
        elif data[0] == "systemctl_cancel":
            await query.edit_message_text("Operation cancelled.")

    def _register_command_handlers(self, application: telegram.ext.Application) -> None:
        application.add_handler(CommandHandler("start", self.start))
        application.add_handler(CommandHandler("help", self.start))
        application.add_handler(CommandHandler("browse", self.browse))
        application.add_handler(CommandHandler("system", self.system_resource_monitoring))
        application.add_handler(CommandHandler("processes", self.list_processes))
        application.add_handler(CommandHandler("kill", self.kill_process))
        application.add_handler(CommandHandler("screenshot", self.send_screenshot))
        application.add_handler(CommandHandler("upload", self.upload_info))
        application.add_handler(CommandHandler("list_uploads", self.list_uploads))
        application.add_handler(CommandHandler("login", self.login))
        application.add_handler(CommandHandler("logout", self.logout))
        application.add_handler(CommandHandler("systemctl", self.systemctl_command))

    def _register_message_handlers(self, application: telegram.ext.Application) -> None:
        application.add_handler(MessageHandler(filters.Document.ALL, self.handle_file_upload))
        application.add_handler(MessageHandler(filters.PHOTO, self.handle_file_upload))
        application.add_handler(MessageHandler(filters.VIDEO, self.handle_file_upload))
        application.add_handler(MessageHandler(filters.AUDIO, self.handle_file_upload))
        application.add_handler(MessageHandler(filters.VOICE, self.handle_file_upload))
        application.add_handler(MessageHandler(filters.VIDEO_NOTE, self.handle_file_upload))

    def _register_cb_query_handlers(self, application: telegram.ext.Application) -> None:
        application.add_handler(CallbackQueryHandler(self.handle_systemctl_confirmation, pattern="^systemctl_"))
        application.add_handler(CallbackQueryHandler(self.handle_navigation))

    def _build_app(self) -> telegram.ext.Application:
        application = ApplicationBuilder().token(self._bot_token).build()
        self._register_command_handlers(application)
        self._register_message_handlers(application)
        self._register_cb_query_handlers(application)

        return application

    def _error_handler(self, _update: object, context: telegram.ext.CallbackContext):
        try:
            raise context.error
        except Exception as exc:
            print_error(f"Exception occurred: {exc}")

    async def run_forever(self) -> NoReturn:
        await self._application.updater.bot.set_my_commands([BotCommand(k, v) for k, v in COMMANDS_DICT.items()])

        try:
            print_info("Initializing application...")
            await self._application.initialize()
            await self._application.start()
            print_info("Starting updater polling...")
            await self._application.updater.start_polling(error_callback=self._error_handler)
            await asyncio.Event().wait()
        except KeyboardInterrupt:
            print_info("Stopping...")
            await self._application.updater.stop()
            await self._application.stop()
        except telegram.error.InvalidToken:
            print_error("bad token - make sure you set a correct bot token in `config.json`")
        except httpcore.ConnectTimeout:
            print_error("Connection timeout")
        finally:
            try:
                print_info("Shutting down...")
                await self._application.shutdown()
            except RuntimeError as exc:
                pass  # ignore 'RuntimeError: This Application is still running!'


async def main() -> NoReturn:
    config_path = Path(__file__).resolve().parent / "config.json"
    conf = load_config(config_path)
    tamer = SysTamer(conf)
    await tamer.run_forever()


if __name__ == '__main__':
    invalidate_print()
    printf(f"\n{BANNER}\n"
           f"Written by {BOLD}@flashnuke{RESET}")
    printf(DELIM)
    try:
        asyncio.get_event_loop().run_until_complete(main())
    except KeyboardInterrupt:
        pass
    finally:
        pass

