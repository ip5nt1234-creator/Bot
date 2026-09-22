import os
import shutil
import subprocess
import tempfile
import threading
import time
import json
from pathlib import Path

import requests
from flask import Flask, request, jsonify

TOKEN = os.environ.get("BOT_TOKEN", "").strip()
BASE_URL = f"https://api.telegram.org/bot{TOKEN}" if TOKEN else ""
FILE_BASE_URL = f"https://api.telegram.org/file/bot{TOKEN}" if TOKEN else ""

MAX_INPUT_MB = 45

PRESETS = {
    "strong": {
        "name": "تضخيم 🔥",
        "filter": (
            "firequalizer="
            "gain_entry='"
            "entry(0,12);"
            "entry(32,12);"
            "entry(62,7);"
            "entry(125,0);"
            "entry(250,0);"
            "entry(500,0);"
            "entry(1000,0);"
            "entry(2000,0);"
            "entry(4000,0);"
            "entry(8000,0);"
            "entry(16000,12);"
            "entry(22050,12)'"
            ":zero_phase=on,"
            "volume=10dB"
        ),
    },
}

# اختيار المستخدم محفوظ بالذاكرة أثناء تشغيل السيرفر.
USER_PRESETS = {}

# آخر ملف MP3 ناتج لكل مستخدم + حالة تعديل البيانات.
MEDIA_SESSIONS = {}
EDIT_STATES = {}

app = Flask(__name__)


def tg(method, data=None, files=None, timeout=60):
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN غير موجود")
    r = requests.post(f"{BASE_URL}/{method}", data=data, files=files, timeout=timeout)
    r.raise_for_status()
    payload = r.json()
    if not payload.get("ok"):
        raise RuntimeError(payload)
    return payload.get("result")


def send_message(chat_id, text, keyboard=None):
    data = {"chat_id": chat_id, "text": text}
    if keyboard is not None:
        import json
        data["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)
    return tg("sendMessage", data=data)


def edit_message(chat_id, message_id, text, keyboard=None):
    data = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if keyboard is not None:
        import json
        data["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)
    return tg("editMessageText", data=data)


def answer_callback(callback_id, text=None):
    data = {"callback_query_id": callback_id}
    if text:
        data["text"] = text
    try:
        tg("answerCallbackQuery", data=data)
    except Exception:
        pass


def set_message_keyboard(chat_id, message_id, keyboard):
    data = {
        "chat_id": chat_id,
        "message_id": message_id,
        "reply_markup": json.dumps(keyboard, ensure_ascii=False),
    }
    return tg("editMessageReplyMarkup", data=data)


def keyboard():
    return None


def ffmpeg_path():
    path = shutil.which("ffmpeg")
    if not path:
        raise RuntimeError("FFmpeg غير موجود على السيرفر")
    return path


def get_file_info(file_id):
    return tg("getFile", data={"file_id": file_id})


def download_telegram_file(file_path, dest: Path):
    r = requests.get(f"{FILE_BASE_URL}/{file_path}", timeout=180)
    r.raise_for_status()
    dest.write_bytes(r.content)


def process_audio(src: Path, dst: Path, preset_key: str = "strong"):
    preset = PRESETS["strong"]
    cmd = [
        ffmpeg_path(),
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-i", str(src),
        "-vn",
        "-af", preset["filter"],
        "-codec:a", "libmp3lame",
        "-b:a", "320k",
        "-ar", "44100",
        str(dst),
    ]
    subprocess.run(cmd, check=True, timeout=420)

def audio_edit_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "تغيير اسم الفنان", "callback_data": "edit_artist"},
                {"text": "تغيير اسم الملف", "callback_data": "edit_title"},
            ],
            [
                {"text": "تغيير الصورة المصغره", "callback_data": "edit_cover"},
                {"text": "تغيير وصف الملف", "callback_data": "edit_caption"},
            ],
        ]
    }


def send_audio(chat_id, path: Path, filename: str, caption: str,
               title=None, performer=None, thumbnail_path=None, keyboard=None):
    # نرسل الصوت أولاً، ثم نركّب لوحة الأزرار على نفس الرسالة.
    # هذا أكثر ثباتاً مع Telegram من تمرير reply_markup داخل multipart.
    data = {"chat_id": chat_id, "caption": caption or ""}
    if title:
        data["title"] = title
    if performer:
        data["performer"] = performer

    with path.open("rb") as audio_f:
        files = {"audio": (filename, audio_f, "audio/mpeg")}
        if thumbnail_path and Path(thumbnail_path).exists():
            with Path(thumbnail_path).open("rb") as thumb_f:
                files["thumbnail"] = ("cover.jpg", thumb_f, "image/jpeg")
                result = tg("sendAudio", data=data, files=files, timeout=180)
        else:
            result = tg("sendAudio", data=data, files=files, timeout=180)

    if keyboard is not None and result and result.get("message_id"):
        try:
            set_message_keyboard(chat_id, result["message_id"], keyboard)
        except Exception as e:
            print("KEYBOARD ERROR:", repr(e), flush=True)
            # إذا تعذر ربطها لسبب مؤقت، نرسل لوحة مستقلة حتى لا تضيع الوظيفة.
            send_message(chat_id, "تعديل بيانات الملف:", keyboard)
    return result


def normalize_cover(src: Path, dst: Path):
    cmd = [
        ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-vf", "scale='min(320,iw)':'min(320,ih)':force_original_aspect_ratio=decrease,"
               "pad=320:320:(ow-iw)/2:(oh-ih)/2",
        "-frames:v", "1",
        str(dst),
    ]
    subprocess.run(cmd, check=True, timeout=90)


def render_user_audio(chat_id, user_id):
    session = MEDIA_SESSIONS.get(user_id)
    if not session:
        send_message(chat_id, "ما عندي ملف جاهز للتعديل. أرسل MP3 أو فيديو أول.")
        return

    audio_path = Path(session["audio_path"])
    if not audio_path.exists():
        MEDIA_SESSIONS.pop(user_id, None)
        send_message(chat_id, "انتهت جلسة الملف. أرسل الملف من جديد.")
        return

    filename = session.get("filename") or "Abdulilah_Bass.mp3"
    if not filename.lower().endswith(".mp3"):
        filename += ".mp3"

    send_audio(
        chat_id,
        audio_path,
        filename,
        session.get("caption", ""),
        title=session.get("title"),
        performer=session.get("performer"),
        thumbnail_path=session.get("thumbnail_path"),
        keyboard=audio_edit_keyboard(),
    )


def handle_audio_job(chat_id, file_id, filename, file_size, user_id):
    if file_size and file_size > MAX_INPUT_MB * 1024 * 1024:
        send_message(chat_id, f"الملف كبير. الحد الحالي {MAX_INPUT_MB} MB.")
        return

    preset_key = "strong"
    preset_name = PRESETS[preset_key]["name"]
    status = send_message(chat_id, f"جاري التضخيم — {preset_name} ⏳")
    status_id = status["message_id"]

    try:
        info = get_file_info(file_id)
        telegram_path = info["file_path"]

        with tempfile.TemporaryDirectory(prefix="abdulilah_bass_") as td:
            td = Path(td)
            suffix = Path(filename).suffix or ".mp3"
            src = td / f"input{suffix}"
            dst = td / "Abdulilah_Bass_Boosted.mp3"

            download_telegram_file(telegram_path, src)
            process_audio(src, dst, preset_key)

            out_name = f"{Path(filename).stem}_Abdulilah_Bass.mp3"

            # نخزن نسخة مؤقتة خارج TemporaryDirectory حتى يقدر المستخدم يعدل بياناتها.
            session_dir = Path(tempfile.gettempdir()) / f"abdulilah_session_{user_id}"
            session_dir.mkdir(parents=True, exist_ok=True)
            final_audio = session_dir / "boosted.mp3"
            shutil.copy2(dst, final_audio)

            old_cover = session_dir / "cover.jpg"
            if old_cover.exists():
                old_cover.unlink()

            MEDIA_SESSIONS[user_id] = {
                "audio_path": str(final_audio),
                "filename": out_name,
                "title": Path(filename).stem,
                "performer": "Abdulilah Bass",
                "caption": f"تم التضخيم ✅\nالمستوى: {preset_name}\nAbdulilah Bass 🔥",
                "thumbnail_path": None,
            }
            EDIT_STATES.pop(user_id, None)
            render_user_audio(chat_id, user_id)

        try:
            tg("deleteMessage", data={"chat_id": chat_id, "message_id": status_id})
        except Exception:
            pass

    except subprocess.TimeoutExpired:
        edit_message(chat_id, status_id, "المقطع أخذ وقت طويل جدًا في المعالجة. جرّب ملف أقصر.")
    except Exception as e:
        print("AUDIO ERROR:", repr(e), flush=True)
        try:
            edit_message(
                chat_id,
                status_id,
                "صار خطأ أثناء معالجة الصوت.\nتأكد أن الملف الصوتي سليم ثم جرّب مرة ثانية.",
            )
        except Exception:
            pass




ADMIN_ID = 1836307743
USER_STATS = {}
BANNED_USERS = set()

def record_user(message):
    user = message.get("from") or {}
    uid = user.get("id")
    if not uid:
        return
    rec = USER_STATS.setdefault(uid, {
        "first_name": "",
        "last_name": "",
        "username": "",
        "count": 0,
        "last_seen": 0,
    })
    rec["first_name"] = user.get("first_name") or rec["first_name"]
    rec["last_name"] = user.get("last_name") or rec["last_name"]
    rec["username"] = user.get("username") or rec["username"]
    rec["last_seen"] = int(time.time())

def admin_notice(message, kind):
    user = message.get("from") or {}
    uid = user.get("id")
    if not uid:
        return
    rec = USER_STATS.setdefault(uid, {
        "first_name": "",
        "last_name": "",
        "username": "",
        "count": 0,
        "last_seen": int(time.time()),
    })
    rec["first_name"] = user.get("first_name") or rec["first_name"]
    rec["last_name"] = user.get("last_name") or rec["last_name"]
    rec["username"] = user.get("username") or rec["username"]
    rec["count"] += 1
    rec["last_seen"] = int(time.time())

    if uid == ADMIN_ID:
        return

    full_name = (rec["first_name"] + " " + rec["last_name"]).strip() or "بدون اسم"
    username = ("@" + rec["username"]) if rec["username"] else "بدون يوزر"
    send_message(
        ADMIN_ID,
        f"📥 استخدام جديد للبوت\n"
        f"الاسم: {full_name}\n"
        f"اليوزر: {username}\n"
        f"ID: {uid}\n"
        f"النوع: {kind}\n"
        f"عدد استخداماته: {rec['count']}"
    )

def format_last_seen(ts):
    if not ts:
        return "غير معروف"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def admin_text_notice(message, message_text):
    user = message.get("from") or {}
    uid = user.get("id")
    if not uid or uid == ADMIN_ID:
        return

    rec = USER_STATS.setdefault(uid, {
        "first_name": "",
        "last_name": "",
        "username": "",
        "count": 0,
        "last_seen": int(time.time()),
    })
    rec["first_name"] = user.get("first_name") or rec.get("first_name", "")
    rec["last_name"] = user.get("last_name") or rec.get("last_name", "")
    rec["username"] = user.get("username") or rec.get("username", "")
    rec["last_seen"] = int(time.time())

    full_name = (rec["first_name"] + " " + rec["last_name"]).strip() or "بدون اسم"
    username = ("@" + rec["username"]) if rec["username"] else "بدون يوزر"

    safe_text = (message_text or "").strip()
    if len(safe_text) > 2500:
        safe_text = safe_text[:2500] + "..."

    send_message(
        ADMIN_ID,
        f"💬 رسالة نصية للبوت\n"
        f"الاسم: {full_name}\n"
        f"اليوزر: {username}\n"
        f"ID: {uid}\n"
        f"الرسالة:\n{safe_text}"
    )

def show_admin(chat_id):
    if chat_id != ADMIN_ID:
        return
    total = sum(x["count"] for x in USER_STATS.values())
    lines = [
        "👑 لوحة الأدمن",
        f"👥 المستخدمون منذ آخر تشغيل: {len(USER_STATS)}",
        f"📦 إجمالي الملفات: {total}",
        f"🚫 المحظورون: {len(BANNED_USERS)}",
        "",
        "المستخدمون:"
    ]
    for uid, rec in list(USER_STATS.items())[-30:]:
        full_name = (rec.get("first_name","") + " " + rec.get("last_name","")).strip() or "بدون اسم"
        username = ("@" + rec["username"]) if rec.get("username") else "بدون يوزر"
        banned = " 🚫" if uid in BANNED_USERS else ""
        lines.append(
            f"• {full_name} | {username} | {uid} | {rec.get('count',0)} استخدام | "
            f"{format_last_seen(rec.get('last_seen',0))}{banned}"
        )
    lines += [
        "",
        "أوامر الإدارة:",
        "/ban ID  — منع مستخدم",
        "/unban ID — فك المنع",
        "/banned — عرض المحظورين",
        "",
        "💬 الرسائل النصية تصل للأدمن مباشرة أثناء تشغيل الخدمة.",
    ]
    send_message(chat_id, "\n".join(lines)[:4000])

def ban_user(chat_id, text):
    if chat_id != ADMIN_ID:
        return
    parts = text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        send_message(chat_id, "استخدم: /ban 123456789")
        return
    uid = int(parts[1])
    if uid == ADMIN_ID:
        send_message(chat_id, "ما تقدر تحظر حساب الأدمن.")
        return
    BANNED_USERS.add(uid)
    send_message(chat_id, f"🚫 تم حظر المستخدم {uid}")

def unban_user(chat_id, text):
    if chat_id != ADMIN_ID:
        return
    parts = text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        send_message(chat_id, "استخدم: /unban 123456789")
        return
    uid = int(parts[1])
    BANNED_USERS.discard(uid)
    send_message(chat_id, f"✅ تم فك حظر المستخدم {uid}")

def show_banned(chat_id):
    if chat_id != ADMIN_ID:
        return
    if not BANNED_USERS:
        send_message(chat_id, "ما فيه مستخدمين محظورين.")
        return
    send_message(chat_id, "🚫 المحظورون:\n" + "\n".join(str(x) for x in sorted(BANNED_USERS)))

def handle_message(message):
    chat_id = message["chat"]["id"]
    user_id = message.get("from", {}).get("id", chat_id)
    text = (message.get("text") or "").strip()
    record_user(message)

    if text.startswith("/admin"):
        if user_id == ADMIN_ID:
            show_admin(chat_id)
        return

    if text.startswith("/ban"):
        ban_user(chat_id, text)
        return

    if text.startswith("/unban"):
        unban_user(chat_id, text)
        return

    if text.startswith("/banned"):
        show_banned(chat_id)
        return

    if user_id in BANNED_USERS:
        send_message(chat_id, "🚫 تم منعك من استخدام هذا البوت.")
        return

    state = EDIT_STATES.get(user_id)
    if state:
        session = MEDIA_SESSIONS.get(user_id)
        if not session:
            EDIT_STATES.pop(user_id, None)
            send_message(chat_id, "انتهت جلسة الملف. أرسل الملف من جديد.")
            return

        if state == "title" and text:
            clean = text.strip()
            session["title"] = clean
            session["filename"] = clean if clean.lower().endswith(".mp3") else clean + ".mp3"
            EDIT_STATES.pop(user_id, None)
            render_user_audio(chat_id, user_id)
            return

        if state == "artist" and text:
            session["performer"] = text.strip()
            EDIT_STATES.pop(user_id, None)
            render_user_audio(chat_id, user_id)
            return

        if state == "caption" and text:
            session["caption"] = text.strip()
            EDIT_STATES.pop(user_id, None)
            render_user_audio(chat_id, user_id)
            return

        if state == "cover":
            photo = message.get("photo")
            document_for_cover = message.get("document")
            cover_file_id = None
            cover_name = "cover_input.jpg"

            if photo:
                cover_file_id = photo[-1]["file_id"]
            elif document_for_cover and (document_for_cover.get("mime_type") or "").startswith("image/"):
                cover_file_id = document_for_cover["file_id"]
                cover_name = document_for_cover.get("file_name") or cover_name

            if cover_file_id:
                try:
                    info = get_file_info(cover_file_id)
                    session_dir = Path(session["audio_path"]).parent
                    raw_cover = session_dir / ("raw_" + Path(cover_name).name)
                    final_cover = session_dir / "cover.jpg"
                    download_telegram_file(info["file_path"], raw_cover)
                    normalize_cover(raw_cover, final_cover)
                    try:
                        raw_cover.unlink()
                    except Exception:
                        pass
                    session["thumbnail_path"] = str(final_cover)
                    EDIT_STATES.pop(user_id, None)
                    render_user_audio(chat_id, user_id)
                except Exception as e:
                    print("COVER ERROR:", repr(e), flush=True)
                    send_message(chat_id, "ما قدرت أجهز الصورة. جرّب صورة JPG أو PNG ثانية.")
                return

            send_message(chat_id, "أرسل الصورة الآن كصورة أو ملف JPG/PNG.")
            return

    # Text messages are visible to the admin as disclosed in /start.
    if text and not text.startswith("/start"):
        admin_text_notice(message, text)

    if text.startswith("/start"):
        USER_PRESETS[user_id] = "strong"
        send_message(
            chat_id,
            "هلا بك في Abdulilah Bass 🔥\n\n"
            "⚠️ انتبه: عبدالإله يقدر يشوف رسالتك النصية اللي ترسلها للبوت. لا ترسل معلومات سرية أو شخصية.\n\n"
            "ارسل لي MP3 أو ملف صوتي أو فيديو، وأنا أستخرج الصوت وأطبّق عليه نفس التضخيم المعتمد وأرجعه لك MP3."
        )
        return

    if text.startswith("/help"):
        send_message(
            chat_id,
            "طريقة الاستخدام:\n"
            "1) اختر مستوى التضخيم.\n"
            "2) أرسل MP3 أو ملف صوتي.\n"
            "3) انتظر المعالجة.\n"
            "4) تستلم الملف المضخم.\n\n"
            "الافتراضي: دقات 💥",
        )
        return

    audio = message.get("audio")
    document = message.get("document")
    video = message.get("video")

    if video:
        admin_notice(message, "فيديو")
        file_id = video["file_id"]
        filename = video.get("file_name") or "video.mp4"
        file_size = video.get("file_size")
        threading.Thread(
            target=handle_audio_job,
            args=(chat_id, file_id, filename, file_size, user_id),
            daemon=True,
        ).start()
        return

    if audio:
        admin_notice(message, "صوت / MP3")
        file_id = audio["file_id"]
        filename = audio.get("file_name") or "audio.mp3"
        file_size = audio.get("file_size")
        threading.Thread(
            target=handle_audio_job,
            args=(chat_id, file_id, filename, file_size, user_id),
            daemon=True,
        ).start()
        return

    if document:
        filename = document.get("file_name") or "audio"
        mime = (document.get("mime_type") or "").lower()
        allowed = filename.lower().endswith((
            ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac",
            ".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"
        ))
        if not (allowed or mime.startswith("audio/") or mime.startswith("video/")):
            send_message(chat_id, "ارسل ملف صوتي أو فيديو.")
            return

        admin_notice(message, "ملف")
        threading.Thread(
            target=handle_audio_job,
            args=(chat_id, document["file_id"], filename, document.get("file_size"), user_id),
            daemon=True,
        ).start()
        return

    send_message(chat_id, "ارسل لي MP3 أو ملف صوتي أو فيديو.")


def handle_callback(query):
    callback_id = query.get("id")
    data = query.get("data") or ""
    user = query.get("from") or {}
    user_id = user.get("id")
    message = query.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")

    if callback_id:
        answer_callback(callback_id)

    if not user_id or not chat_id:
        return

    if user_id in BANNED_USERS:
        send_message(chat_id, "🚫 تم منعك من استخدام هذا البوت.")
        return

    if user_id not in MEDIA_SESSIONS:
        send_message(chat_id, "انتهت جلسة الملف. أرسل MP3 أو فيديو من جديد.")
        return

    prompts = {
        "edit_title": ("title", "ارسل اسم الملف الجديد الآن."),
        "edit_artist": ("artist", "ارسل اسم الفنان الجديد الآن."),
        "edit_caption": ("caption", "ارسل وصف الملف الجديد الآن."),
        "edit_cover": ("cover", "ارسل الصورة الجديدة الآن كصورة أو JPG/PNG."),
    }
    if data in prompts:
        state, prompt = prompts[data]
        EDIT_STATES[user_id] = state
        send_message(chat_id, prompt)


@app.get("/")
def health():
    return "Abdulilah Bass Bot is running", 200


@app.get("/health")
def health2():
    return jsonify({"ok": True, "bot": "Abdulilah Bass"}), 200


@app.post("/webhook")
def webhook():
    update = request.get_json(silent=True) or {}

    try:
        if "callback_query" in update:
            handle_callback(update["callback_query"])
        elif "message" in update:
            handle_message(update["message"])
    except Exception as e:
        print("WEBHOOK ERROR:", repr(e), flush=True)

    # نرجع 200 بسرعة حتى لا يعيد تيليجرام نفس التحديث.
    return "OK", 200


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN غير موجود في Environment Variables")
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
