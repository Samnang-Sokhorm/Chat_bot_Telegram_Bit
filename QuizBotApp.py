# quiz_app.py
import sys
import os
import json
import time
import threading
import asyncio
import logging

from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QTextEdit, QMessageBox, QComboBox, QDialog,
    QTabWidget, QLineEdit, QListWidget
)
from PyQt5.QtGui import QFont
from PyQt5.QtCore import Qt, QObject, pyqtSignal

from telegram import Poll, Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, PollAnswerHandler, ContextTypes
)

# ---------------- logging ----------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("QuizApp")
# ---------------- constants ----------------
if getattr(sys, 'frozen', False):
    # Running as compiled executable
    BASE_DIR = os.path.dirname(sys.executable)
else:
    # Running as script
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------- constants ----------------
KHMER_FONT = "Khmer OS Siemreap"
FONT_SIZE = 12
QUESTIONS_FILE = "questions.txt"
CHAT_IDS_FILE = "active_chats.txt"  # File to store active chat IDs

# ---------------- small helper UI widgets ----------------
class BotSignals(QObject):
    log_message = pyqtSignal(str)
    status_update = pyqtSignal(str)
    chat_registered = pyqtSignal(int, str)  # chat_id, chat_title


class KhmerTextEdit(QTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFont(QFont(KHMER_FONT, FONT_SIZE))
        self.setPlaceholderText("Type your Khmer text here...")
        self.setAcceptRichText(False)
        self.setAttribute(Qt.WA_InputMethodEnabled, True)


class QuestionDialog(QDialog):
    def __init__(self, parent=None, data=None):
        super().__init__(parent)
        self.result = None
        self.setWindowTitle("Question Editor")
        self.setGeometry(200, 200, 700, 500)
        layout = QVBoxLayout()

        layout.addWidget(QLabel("Question:"))
        self.question_edit = KhmerTextEdit()
        if data:
            self.question_edit.setPlainText(data.get("question", ""))
        layout.addWidget(self.question_edit)

        self.option_edits = []
        for i in range(4):
            layout.addWidget(QLabel(f"Option {i+1}:"))
            e = KhmerTextEdit()
            e.setMaximumHeight(60)
            if data and i < len(data.get("options", [])):
                e.setPlainText(data["options"][i])
            self.option_edits.append(e)
            layout.addWidget(e)

        layout.addWidget(QLabel("Correct option:"))
        self.correct_combo = QComboBox()
        self.correct_combo.addItems([f"Option {i+1}" for i in range(4)])
        if data:
            self.correct_combo.setCurrentIndex(data.get("correct", 0))
        layout.addWidget(self.correct_combo)

        btn_layout = QHBoxLayout()
        ok = QPushButton("OK")
        ok.clicked.connect(self.accept)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        btn_layout.addWidget(ok)
        btn_layout.addWidget(cancel)
        layout.addLayout(btn_layout)
        self.setLayout(layout)

    def accept(self):
        question = self.question_edit.toPlainText().strip()
        options = [e.toPlainText().strip() for e in self.option_edits]
        if not question:
            QMessageBox.warning(self, "Warning", "Please enter a question")
            return
        non_empty = [o for o in options if o]
        if len(non_empty) < 2:
            QMessageBox.warning(self, "Warning", "Please enter at least 2 options")
            return
        correct_index = self.correct_combo.currentIndex()
        if not options[correct_index]:
            QMessageBox.warning(self, "Warning", "Correct option cannot be empty")
            return
        self.result = {"question": question, "options": options, "correct": correct_index}
        super().accept()


class TelegramQuizBot:
    def __init__(self, token: str, questions_file: str = QUESTIONS_FILE, signals: BotSignals = None):
        self.token = token
        self.questions_file = questions_file
        self.signals = signals or BotSignals()
        self.application = None
        self.loop = None
        self.is_running = False

        # data
        self.QUIZ_QUESTIONS = []
        self.load_questions()

        # Active chats management
        self.registered_chats = {}  # chat_id -> {"title": str, "active": bool}
        self.load_chats()

        # state dictionaries
        self.user_progress = {}       # (chat_id, user_id) -> index
        self.user_scores = {}         # (chat_id, user_id) -> pts
        self.user_names = {}          # (chat_id, user_id) -> display name
        self.user_start_time = {}     # (chat_id, user_id) -> epoch

        # per-chat group state
        self.chat_state = {}

        # quick lookup: poll_id -> chat_id (active polls)
        self.poll_to_chat = {}

        # per-chat timeout tasks (asyncio.Task)
        self.chat_timeout_tasks = {}

    def load_questions(self):
        try:
            with open(self.questions_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            valid = []
            for q in data:
                if isinstance(q, dict) and q.get("question") and isinstance(q.get("options", []), list) and len(q.get("options", [])) >= 2:
                    valid.append({
                        "question": str(q["question"]).strip(),
                        "options": [str(o).strip() for o in q["options"]],
                        "correct": int(q.get("correct", 0))
                    })
            self.QUIZ_QUESTIONS = valid
            self.signals.log_message.emit(f"Loaded {len(valid)} questions")
        except Exception as e:
            logger.exception("Failed to load questions")
            self.signals.log_message.emit(f"Error loading questions: {e}")
            self.QUIZ_QUESTIONS = []

    def load_chats(self):
        """Load previously registered chats from file"""
        try:
            with open(CHAT_IDS_FILE, "r", encoding="utf-8") as f:
                self.registered_chats = json.load(f)
            self.signals.log_message.emit(f"Loaded {len(self.registered_chats)} registered chats")
        except FileNotFoundError:
            self.registered_chats = {}
        except Exception as e:
            logger.exception("Failed to load chats")
            self.registered_chats = {}

    def save_chats(self):
        """Save registered chats to file"""
        try:
            with open(CHAT_IDS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.registered_chats, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.exception("Failed to save chats")

    def reload_questions(self):
        self.load_questions()

    async def register_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command - automatically register chat and start quiz"""
        chat_id = update.effective_chat.id
        chat_title = update.effective_chat.title or "Private Chat"
        user = update.effective_user
        
        # Register chat
        self.registered_chats[str(chat_id)] = {
            "title": chat_title,
            "active": True,
            "registered_by": user.first_name or "Unknown",
            "registered_at": time.time()
        }
        self.save_chats()
        
        # Notify GUI
        self.signals.chat_registered.emit(chat_id, chat_title)
        self.signals.log_message.emit(f"Chat registered: {chat_title} (ID: {chat_id})")
        
        # Send welcome message
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🎉 Welcome to Quiz Bot, {chat_title}!\n\n"
                 f"📝 I'll automatically start the quiz now. Get ready!\n"
                 f"✅ You're registered and ready to play."
        )

        # Start quiz immediately
        await self._start_quiz_coroutine(chat_id)

    async def _start_quiz_coroutine(self, chat_id: int):
        """Start quiz in a specific chat"""
        if not getattr(self, "application", None) or not getattr(self, "loop", None):
            raise RuntimeError("Bot not running. Start the bot first (Start Bot).")

        class Ctx:
            pass
        ctx = Ctx()
        ctx.bot = self.application.bot
        
        try:
            await ctx.bot.send_message(chat_id=chat_id, text="📢 Quiz starting now! Get ready...")
        except Exception as e:
            logger.warning(f"Could not announce to chat {chat_id}: {e}")
            return

        # Initialize chat state
        self.chat_state.setdefault(chat_id, {"q_index": 0, "poll_id": None, "responses": {}})
        
        # Start the quiz
        await self.ask_question(ctx, chat_id)

    def start_quiz_in_chat(self, chat_id: int):
        """Called from GUI thread to start quiz in specific chat"""
        if not getattr(self, "application", None) or not getattr(self, "loop", None):
            raise RuntimeError("Bot not running. Start the bot first (Start Bot).")

        fut = asyncio.run_coroutine_threadsafe(self._start_quiz_coroutine(chat_id), self.loop)
        return fut

    def start_quiz_all_chats(self):
        """Start quiz in all registered chats"""
        if not getattr(self, "application", None) or not getattr(self, "loop", None):
            raise RuntimeError("Bot not running. Start the bot first (Start Bot).")

        for chat_id_str in self.registered_chats.keys():
            if self.registered_chats[chat_id_str].get("active", True):
                try:
                    chat_id = int(chat_id_str)
                    self.start_quiz_in_chat(chat_id)
                except (ValueError, KeyError) as e:
                    logger.error(f"Invalid chat ID {chat_id_str}: {e}")

    # ... (keep all the existing methods: ask_question, _poll_timeout_task, handle_poll_answer, show_leaderboard_group)

    async def ask_question(self, context, chat_id: int):
        """Posts the current question for the group as a single poll"""
        if not self.QUIZ_QUESTIONS:
            logger.info("No questions loaded.")
            try:
                await context.bot.send_message(chat_id=chat_id, text="No questions available. Please load questions.")
            except Exception:
                pass
            return

        state = self.chat_state.setdefault(chat_id, {"q_index": 0, "poll_id": None, "responses": {}})
        q_index = state["q_index"]

        if q_index >= len(self.QUIZ_QUESTIONS):
            await self.show_leaderboard_group(chat_id, context)
            return

        q_data = self.QUIZ_QUESTIONS[q_index]

        poll_msg = await context.bot.send_poll(
            chat_id=chat_id,
            question=f"Q{q_index+1}: {q_data['question']}",
            options=q_data["options"],
            type=Poll.QUIZ,
            correct_option_id=q_data["correct"],
            is_anonymous=False,
            open_period=30
        )

        poll_id = poll_msg.poll.id
        logger.info(f"Sent poll {poll_id} for chat {chat_id} Q{q_index+1}")
        state["poll_id"] = poll_id
        state["responses"] = {}
        self.poll_to_chat[poll_id] = chat_id
        self.user_start_time.setdefault((chat_id, 0), time.time())

        old_task = self.chat_timeout_tasks.get(chat_id)
        if old_task and not old_task.done():
            try:
                old_task.cancel()
            except Exception:
                pass

        task = asyncio.create_task(self._poll_timeout_task(poll_id))
        self.chat_timeout_tasks[chat_id] = task

    async def _poll_timeout_task(self, poll_id: str):
        """Wait 30 seconds then finalize the poll"""
        await asyncio.sleep(30)

        chat_id = self.poll_to_chat.get(poll_id)
        if chat_id is None:
            return

        state = self.chat_state.get(chat_id)
        if not state or state.get("poll_id") != poll_id:
            return

        q_index = state["q_index"]
        q_data = self.QUIZ_QUESTIONS[q_index]
        responses = state.get("responses", {})

        participants = {k for k in self.user_progress.keys() if k[0] == chat_id}
        for uid in responses.keys():
            participants.add((chat_id, uid))
            if (chat_id, uid) not in self.user_progress:
                self.user_progress[(chat_id, uid)] = 0
                self.user_scores[(chat_id, uid)] = 0
                self.user_names[(chat_id, uid)] = "Anonymous"
                self.user_start_time[(chat_id, uid)] = time.time()

        for key in participants:
            _, uid = key
            if uid in responses:
                selected = responses[uid]
                if selected == q_data["correct"]:
                    self.user_scores[key] = self.user_scores.get(key, 0) + 10
                    try:
                        await self.application.bot.send_message(chat_id=uid, text=f"✅ Correct! +10 pts. Total: {self.user_scores[key]} pts")
                    except Exception:
                        logger.debug("Could not DM correct feedback")
                else:
                    correct_text = q_data["options"][q_data["correct"]]
                    try:
                        await self.application.bot.send_message(chat_id=uid, text=f"❌ Wrong. Correct: {correct_text}\nTotal: {self.user_scores.get(key,0)} pts")
                    except Exception:
                        logger.debug("Could not DM wrong feedback")
            else:
                try:
                    await self.application.bot.send_message(chat_id=uid, text=f"⏰ Time's up — you skipped Q{q_index+1}. No points.")
                except Exception:
                    logger.debug("Could not DM skipped user")

            self.user_progress[key] = self.user_progress.get(key, 0) + 1

        try:
            del self.poll_to_chat[poll_id]
        except KeyError:
            pass

        state["q_index"] = q_index + 1
        state["poll_id"] = None
        state["responses"] = {}

        await asyncio.sleep(0.8)
        await self.ask_question(self.application, chat_id)

    async def handle_poll_answer(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        poll_answer = update.poll_answer
        poll_id = poll_answer.poll_id
        user = poll_answer.user
        user_id = user.id

        chat_id = self.poll_to_chat.get(poll_id)
        if chat_id is None:
            return

        state = self.chat_state.get(chat_id)
        if not state or state.get("poll_id") != poll_id:
            return

        if user_id in state["responses"]:
            return

        chosen = poll_answer.option_ids[0] if poll_answer.option_ids else -1
        state["responses"][user_id] = chosen

        key = (chat_id, user_id)
        if key not in self.user_progress:
            self.user_progress[key] = 0
            self.user_scores[key] = 0
            self.user_names[key] = user.username or user.first_name or "Anonymous"
            self.user_start_time[key] = time.time()

        participants = [k for k in self.user_progress.keys() if k[0] == chat_id]
        if participants:
            all_answered = all(((cid, uid)[1] in state["responses"]) for cid, uid in participants)
            if all_answered:
                t = self.chat_timeout_tasks.get(chat_id)
                if t and not t.done():
                    try:
                        t.cancel()
                    except Exception:
                        pass
                asyncio.create_task(self._poll_timeout_task(state["poll_id"]))

    async def show_leaderboard_group(self, chat_id: int, context):
        group_entries = {k: v for k, v in self.user_scores.items() if k[0] == chat_id}
        if not group_entries:
            try:
                await context.bot.send_message(chat_id=chat_id, text="No scores yet in this group.")
            except Exception:
                pass
            return

        def sort_key(item):
            (c_id, u_id), score = item
            total_time = time.time() - self.user_start_time.get((c_id, u_id), time.time())
            return (-score, total_time)

        sorted_users = sorted(group_entries.items(), key=sort_key)
        lines = [f"🏆 Group Leaderboard ({len(sorted_users)} players):\n"]
        for i, ((_, uid), score) in enumerate(sorted_users, 1):
            name = self.user_names.get((chat_id, uid), "Unknown")
            percentage = (score / (len(self.QUIZ_QUESTIONS) * 10)) * 100 if self.QUIZ_QUESTIONS else 0
            duration = int(time.time() - self.user_start_time.get((chat_id, uid), time.time()))
            m, s = divmod(duration, 60)
            lines.append(f"{i}. {name} — {score} pts ({percentage:.0f}%) | ⏱ {m}m{s}s")

        text = "\n".join(lines)
        try:
            await context.bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            logger.exception("Failed to send leaderboard")

    def run_bot(self):
        """Start the bot (call in background thread)"""
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

            self.application = ApplicationBuilder().token(self.token).build()
            self.application.add_handler(CommandHandler("start", self.register_command))
            self.application.add_handler(PollAnswerHandler(self.handle_poll_answer))

            self.is_running = True
            self.signals.log_message.emit("Bot started")
            self.signals.status_update.emit("Bot: Running")
            
            self.loop.run_until_complete(self.application.run_polling())
        except Exception as e:
            logger.exception("Bot crashed")
            self.signals.log_message.emit(f"Bot error: {e}")
            self.signals.status_update.emit("Bot: Error")
            self.is_running = False

    def stop_bot(self):
        try:
            if self.application and self.loop:
                asyncio.run_coroutine_threadsafe(self.application.stop(), self.loop)
            self.is_running = False
            self.signals.log_message.emit("Bot stopped")
            self.signals.status_update.emit("Bot: Stopped")
        except Exception:
            logger.exception("Error stopping bot")
            self.is_running = False


class QuizEditor(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Quiz Editor & Telegram Bot")
        self.setGeometry(100, 100, 1100, 700)

        self.questions_file = QUESTIONS_FILE
        self.questions = []
        self.load_questions_file()

        self.signals = BotSignals()
        self.telegram_bot = TelegramQuizBot(token="", 
                                           questions_file=self.questions_file, 
                                           signals=self.signals)

        self.bot_thread = None

        self.setup_ui()
        self.refresh_question_list()
        self.refresh_chat_list()

        self.signals.log_message.connect(self.on_bot_log)
        self.signals.status_update.connect(self.on_bot_status)
        self.signals.chat_registered.connect(self.on_chat_registered)

    def load_questions_file(self):
        try:
            with open(self.questions_file, "r", encoding="utf-8") as f:
                self.questions = json.load(f)
        except Exception:
            with open(self.questions_file, "w", encoding="utf-8") as f:
                json.dump(self.questions, f, ensure_ascii=False, indent=2)

    def save_questions_file(self):
        try:
            with open(self.questions_file, "w", encoding="utf-8") as f:
                json.dump(self.questions, f, ensure_ascii=False, indent=2)
            self.status_label.setText("Saved questions.")
            self.telegram_bot.reload_questions()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save: {e}")

    def setup_ui(self):
        layout = QVBoxLayout(self)

        title = QLabel("Quiz Editor & Bot")
        title.setFont(QFont(KHMER_FONT, 16))
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        tabs = QTabWidget()
        layout.addWidget(tabs)

        # Editor tab
        editor_tab = QWidget()
        tabs.addTab(editor_tab, "Question Editor")
        ed_layout = QVBoxLayout(editor_tab)

        self.list_widget = QListWidget()
        ed_layout.addWidget(self.list_widget)

        btn_layout = QHBoxLayout()
        add_btn = QPushButton("➕ Add")
        add_btn.clicked.connect(self.add_question)
        edit_btn = QPushButton("✏️ Edit")
        edit_btn.clicked.connect(self.edit_question)
        del_btn = QPushButton("🗑 Delete")
        del_btn.clicked.connect(self.delete_question)
        save_btn = QPushButton("💾 Save")
        save_btn.clicked.connect(self.save_questions_file)
        btn_layout.addWidget(add_btn)
        btn_layout.addWidget(edit_btn)
        btn_layout.addWidget(del_btn)
        btn_layout.addWidget(save_btn)
        ed_layout.addLayout(btn_layout)

        self.status_label = QLabel("Ready")
        ed_layout.addWidget(self.status_label)

        # Bot tab
        bot_tab = QWidget()
        tabs.addTab(bot_tab, "Telegram Bot")
        bot_layout = QVBoxLayout(bot_tab)

        self.bot_status_label = QLabel("Bot: Not running")
        bot_layout.addWidget(self.bot_status_label)

        # Bot control buttons
        control_layout = QHBoxLayout()
        start_bot_btn = QPushButton("Start Bot")
        start_bot_btn.clicked.connect(self.start_bot)
        stop_bot_btn = QPushButton("Stop Bot")
        stop_bot_btn.clicked.connect(self.stop_bot)
        control_layout.addWidget(start_bot_btn)
        control_layout.addWidget(stop_bot_btn)
        bot_layout.addLayout(control_layout)

        # Registered chats section
        bot_layout.addWidget(QLabel("📱 Registered Groups/Chats:"))
        self.chat_list_widget = QListWidget()
        bot_layout.addWidget(self.chat_list_widget)

        # Chat management buttons
        chat_btn_layout = QHBoxLayout()
        start_all_btn = QPushButton("Start Quiz in All Chats")
        start_all_btn.clicked.connect(self.start_quiz_all_chats)
        refresh_chats_btn = QPushButton("🔄 Refresh")
        refresh_chats_btn.clicked.connect(self.refresh_chat_list)
        chat_btn_layout.addWidget(start_all_btn)
        chat_btn_layout.addWidget(refresh_chats_btn)
        bot_layout.addLayout(chat_btn_layout)

        # Manual chat ID input (backup option)
        manual_layout = QHBoxLayout()
        manual_layout.addWidget(QLabel("Manual Chat ID:"))
        self.manual_chat_input = QLineEdit()
        self.manual_chat_input.setPlaceholderText("Enter chat ID manually if needed")
        manual_start_btn = QPushButton("Start Quiz in This Chat")
        manual_start_btn.clicked.connect(self.start_quiz_manual)
        manual_layout.addWidget(self.manual_chat_input)
        manual_layout.addWidget(manual_start_btn)
        bot_layout.addLayout(manual_layout)

        bot_layout.addWidget(QLabel("Bot Log:"))
        self.terminal_output = QTextEdit()
        self.terminal_output.setReadOnly(True)
        bot_layout.addWidget(self.terminal_output)

    def refresh_question_list(self):
        self.list_widget.clear()
        for i, q in enumerate(self.questions):
            display = q.get("question", "")[:60]
            item = QListWidgetItem(f"{i+1}. {display}{'...' if len(q.get('question',''))>60 else ''}")
            self.list_widget.addItem(item)

    def refresh_chat_list(self):
        """Refresh the list of registered chats"""
        self.chat_list_widget.clear()
        for chat_id, chat_info in self.telegram_bot.registered_chats.items():
            title = chat_info.get("title", "Unknown")
            status = "✅ Active" if chat_info.get("active", True) else "❌ Inactive"
            item = QListWidgetItem(f"{title} (ID: {chat_id}) - {status}")
            self.chat_list_widget.addItem(item)

    def on_chat_registered(self, chat_id: int, chat_title: str):
        """When a new chat registers via /start command"""
        self.refresh_chat_list()
        self.terminal_output.append(f"✅ New chat registered: {chat_title} (ID: {chat_id})")

    def add_question(self):
        d = QuestionDialog(self)
        if d.exec_() == QDialog.Accepted and d.result:
            self.questions.append(d.result)
            self.refresh_question_list()
            self.status_label.setText("Question added")

    def edit_question(self):
        sel = self.list_widget.selectedItems()
        if not sel:
            QMessageBox.warning(self, "Select", "Select a question first")
            return
        idx = self.list_widget.row(sel[0])
        data = self.questions[idx]
        d = QuestionDialog(self, data)
        if d.exec_() == QDialog.Accepted and d.result:
            self.questions[idx] = d.result
            self.refresh_question_list()
            self.status_label.setText("Question updated")

    def delete_question(self):
        sel = self.list_widget.selectedItems()
        if not sel:
            QMessageBox.warning(self, "Select", "Select a question first")
            return
        idx = self.list_widget.row(sel[0])
        del self.questions[idx]
        self.refresh_question_list()
        self.status_label.setText("Question deleted")

    def on_bot_log(self, message: str):
        self.terminal_output.append(message)

    def on_bot_status(self, status: str):
        self.bot_status_label.setText(status)

    def start_bot(self):
        """Start the Telegram bot"""
        if self.telegram_bot.is_running:
            QMessageBox.information(self, "Info", "Bot already running.")
            return
        self.bot_thread = threading.Thread(target=self.telegram_bot.run_bot, daemon=True)
        self.bot_thread.start()
        self.status_label.setText("Bot starting...")

    def stop_bot(self):
        """Stop the Telegram bot"""
        try:
            self.telegram_bot.stop_bot()
            self.terminal_output.append("Stop requested")
        except Exception as e:
            self.terminal_output.append(f"Stop error: {e}")

    def start_quiz_all_chats(self):
        """Start quiz in all registered chats"""
        if not self.telegram_bot.is_running:
            QMessageBox.warning(self, "Bot not running", "Start the bot first.")
            return
        
        active_chats = [cid for cid, info in self.telegram_bot.registered_chats.items() 
                       if info.get("active", True)]
        
        if not active_chats:
            QMessageBox.information(self, "No chats", "No active chats registered.")
            return
            
        self.telegram_bot.start_quiz_all_chats()
        self.terminal_output.append(f"Started quiz in {len(active_chats)} active chats")

    def start_quiz_manual(self):
        """Start quiz in manually specified chat"""
        chat_id_str = self.manual_chat_input.text().strip()
        if not chat_id_str:
            QMessageBox.warning(self, "Missing", "Enter chat ID")
            return
        
        try:
            chat_id = int(chat_id_str)
        except ValueError:
            QMessageBox.warning(self, "Invalid", "Chat ID must be a number")
            return
            
        if not self.telegram_bot.is_running:
            QMessageBox.warning(self, "Bot not running", "Start the bot first.")
            return
            
        try:
            self.telegram_bot.start_quiz_in_chat(chat_id)
            self.terminal_output.append(f"Started quiz in manual chat: {chat_id}")
        except Exception as e:
            self.terminal_output.append(f"Failed to start quiz: {e}")


if __name__ == "__main__":
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    app = QApplication(sys.argv)
    app.setFont(QFont(KHMER_FONT, FONT_SIZE))
    editor = QuizEditor()
    editor.show()

    sys.exit(app.exec_())
