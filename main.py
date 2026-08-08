import asyncio
import base64
import glob
import json
import logging
import os
import re
import subprocess
import tempfile
import time
import uuid

import yt_dlp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MAX_SEGMENT_SECONDS = int(os.getenv("MAX_SEGMENT_SECONDS", "7200"))
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))
AUDIO_QUALITY = os.getenv("AUDIO_QUALITY", "192")
TEMP_DIR = os.environ.get("TEMP_DIR") or tempfile.gettempdir()
FFMPEG_LOCATION = os.getenv("FFMPEG_LOCATION", "")
ALLOWED_USER_IDS = {
    int(user_id)
    for user_id in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if user_id.strip()
}

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/telegram-webhook")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
PORT = int(os.getenv("PORT", "8080"))

BOT_API_URL = os.getenv("BOT_API_URL", "https://api.telegram.org/bot")
BOT_API_FILE_URL = os.getenv("BOT_API_FILE_URL", "https://api.telegram.org/file/bot")

COOKIES_FILE = ""
_cookies_b64 = os.getenv("COOKIES_TXT_B64", "")
if _cookies_b64:
    _cookies_path = os.path.join(TEMP_DIR, "cookies.txt")
    try:
        with open(_cookies_path, "wb") as _f:
            _f.write(base64.b64decode(_cookies_b64))
        COOKIES_FILE = _cookies_path
    except Exception:
        COOKIES_FILE = ""
elif os.getenv("COOKIES_TXT"):
    _cookies_path = os.path.join(TEMP_DIR, "cookies.txt")
    try:
        with open(_cookies_path, "w", encoding="utf-8") as _f:
            _f.write(os.getenv("COOKIES_TXT"))
        COOKIES_FILE = _cookies_path
    except OSError:
        COOKIES_FILE = ""

BGUTIL_SERVER_HOME = os.getenv("BGUTIL_SERVER_HOME", "")
DENO_PATH = os.getenv("DENO_PATH", "deno")

WAITING_FOR_URL, WAITING_FOR_TIME = range(2)

YOUTUBE_URL_RE = re.compile(
    r"(?:youtube\.com\/(?:watch\?(?:.*&)?v=|shorts\/|embed\/|live\/)|youtu\.be\/)"
    r"([A-Za-z0-9_-]{11})"
)

YOUTUBE_EXTRACTOR_ARGS_FAST = {
    "youtube": {"player_client": ["android_creator"]}
}
YOUTUBE_EXTRACTOR_ARGS_FULL = {
    "youtube": {"player_client": ["android_creator", "tv", "web_safari", "web_embedded"]}
}
YOUTUBE_EXTRACTOR_ARGS_POT = {
    "youtube": {"player_client": ["mweb"]}
}

TIME_RE = re.compile(r"^(?:(\d+):)?([0-5]?\d):([0-5]\d)$")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

if COOKIES_FILE:
    logger.info("تم تحميل ملف الكوكيز: %s (الحجم: %d بايت)", COOKIES_FILE, os.path.getsize(COOKIES_FILE))
else:
    logger.info("لم يتم تحميل أي كوكيز (COOKIES_FILE فارغ)")


def base_ytdlp_options(use_cookies: bool = True) -> dict:
    options = {"quiet": True, "noplaylist": True, "js_runtimes": {"node": {}, "deno": {}}}
    if DENO_PATH:
        options["js_runtimes"]["deno"]["path"] = DENO_PATH
    if BGUTIL_SERVER_HOME:
        options.setdefault("extractor_args", {})["youtubepot-bgutilhttp"] = {
            "base_url": "http://127.0.0.1:4416"
        }
    if use_cookies and COOKIES_FILE:
        options["cookiefile"] = COOKIES_FILE
    return options


def parse_time(value: str) -> int | None:
    match = TIME_RE.match(value.strip())
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)


def format_time(seconds: int) -> str:
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def estimate_mp3_size(seconds: int, quality_kbps: int = 192) -> int:
    return int(seconds * quality_kbps * 1000 / 8)


def underlying_error(exc: Exception) -> str:
    info = getattr(exc, "exc_info", None)
    if info and len(info) > 1 and info[1] is not None:
        try:
            message = str(info[1])
            if message and message != "None":
                return message
        except Exception:
            pass
    return str(exc)


def arabic_error(exc: Exception) -> str:
    message = underlying_error(exc).lower()
    if "private" in message or "members-only" in message:
        return "هذا الفيديو خاص أو للأعضاء فقط ولا يمكن الوصول إليه."
    if "video unavailable" in message or "not available" in message or "removed" in message:
        return "هذا الفيديو غير متاح (قد يكون محذوفاً أو محظوراً في بلدك)."
    if "sign in to confirm" in message or "age-restricted" in message:
        return "هذا الفيديو يتطلب تسجيل الدخول لتأكيد العمر ولا يمكن تنزيله."
    if "copyright" in message:
        return "تمت إزالة هذا الفيديو بسبب حقوق النشر."
    if "live stream" in message or "is live" in message:
        return "لا يمكن قصّ البث المباشر بهذه الطريقة."
    if (
        "timeout" in message
        or "timed out" in message
        or "connection" in message
        or "network" in message
        or "temporary failure" in message
        or "name or service not known" in message
    ):
        return "حدثت مشكلة في الشبكة أو الإنترنت بطيء حالياً. حاول مرة أخرى بعد قليل."
    if "ffmpeg" in message or "ffprobe" in message:
        return "حدث خطأ في معالجة الصوت (FFmpeg). تأكد من تثبيته على الخادم."
    if "exited with code" in message:
        return "توقّف التنزيل أثناء معالجة الصوت (تمت مقاطعته). حاول مرة أخرى."
    if "unsupported url" in message:
        return "الرابط غير مدعوم أو غير صالح."
    if not message or message in ("none", "None"):
        return "حدث خطأ غير متوقع. حاول مرة أخرى."
    return f"حدث خطأ غير متوقع: {message[:500]}"


async def safe_edit(message, text: str) -> None:
    try:
        await message.edit_text(text)
    except Exception:
        pass


async def get_video_info(url: str) -> dict:
    def _fetch():
        def _attempt(extractor_args: dict) -> dict:
            options = base_ytdlp_options()
            options.update({
                "skip_download": True,
                "extractor_args": extractor_args,
            })
            with yt_dlp.YoutubeDL(options) as ydl:
                return ydl.extract_info(url, download=False)

        try:
            return _attempt(YOUTUBE_EXTRACTOR_ARGS_FAST)
        except Exception:
            pass
        try:
            return _attempt(YOUTUBE_EXTRACTOR_ARGS_FULL)
        except Exception:
            pass
        if BGUTIL_SERVER_HOME:
            return _attempt(YOUTUBE_EXTRACTOR_ARGS_POT)
        raise RuntimeError("فشل جلب معلومات الفيديو بكل المحاولات")

    return await asyncio.to_thread(_fetch)


def build_progress_hook(status, loop):
    last_update = {"time": 0.0}

    async def _edit(percent: str):
        await safe_edit(status, f"جاري تنزيل الصوت من يوتيوب...\nاكتمل: {percent}")

    def hook(data: dict) -> None:
        if data.get("status") != "downloading":
            return
        now = time.monotonic()
        if now - last_update["time"] < 3:
            return
        last_update["time"] = now
        percent = (data.get("_percent_str") or "0%").strip()
        asyncio.run_coroutine_threadsafe(_edit(percent), loop)

    return hook


def build_phase_editor(status, loop):
    def edit(text: str) -> None:
        asyncio.run_coroutine_threadsafe(safe_edit(status, text), loop)

    return edit


def ffmpeg_bin() -> str:
    if FFMPEG_LOCATION:
        return os.path.join(FFMPEG_LOCATION, "ffmpeg")
    return "ffmpeg"


async def download_audio(
    url: str,
    uid: str,
    start: int | None = None,
    end: int | None = None,
    progress_hook=None,
    quality_kbps: int = 192,
    phase_cb=None,
    info=None,
) -> str | None:
    def _run() -> str | None:
        is_full = start is None and end is None
        options = base_ytdlp_options()
        options.update({
                "format": "bestaudio[ext=m4a]/bestaudio/best",
                "outtmpl": os.path.join(TEMP_DIR, f"{uid}.%(ext)s"),
                "no_warnings": True,
                "retries": 5,
                "fragment_retries": 5,
                "socket_timeout": 60,
                "concurrent_fragment_downloads": 10,
                "extractor_args": YOUTUBE_EXTRACTOR_ARGS_FAST,
        })
        if FFMPEG_LOCATION:
            options["ffmpeg_location"] = FFMPEG_LOCATION
        if progress_hook:
            options["progress_hooks"] = [progress_hook]
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                if info is not None:
                    ydl.process_ie_result(info, download=True)
                else:
                    ydl.extract_info(url, download=True)
        except Exception as exc:
            if not BGUTIL_SERVER_HOME:
                raise
            logger.warning("فشل التنزيل بالعميل الأساسي (%s)، إعادة بالمشغل mweb+bgutil", exc)
            fallback = base_ytdlp_options()
            fallback.update({
                "format": "bestaudio[ext=m4a]/bestaudio/best",
                "outtmpl": os.path.join(TEMP_DIR, f"{uid}.%(ext)s"),
                "no_warnings": True,
                "retries": 5,
                "fragment_retries": 5,
                "socket_timeout": 60,
                "concurrent_fragment_downloads": 10,
                "extractor_args": YOUTUBE_EXTRACTOR_ARGS_POT,
            })
            if FFMPEG_LOCATION:
                fallback["ffmpeg_location"] = FFMPEG_LOCATION
            if progress_hook:
                fallback["progress_hooks"] = [progress_hook]
            with yt_dlp.YoutubeDL(fallback) as ydl:
                ydl.extract_info(url, download=True)

        matches = sorted(glob.glob(os.path.join(TEMP_DIR, f"{uid}.*")))
        native = next((p for p in matches if not p.endswith(".part")), None)
        if not native:
            return None

        if is_full:
            if os.path.getsize(native) > MAX_UPLOAD_BYTES:
                if phase_cb:
                    phase_cb("تم تنزيل الصوت (100%). جاري تحويله إلى MP3 لضغط الحجم...")
                mp3_path = os.path.join(TEMP_DIR, f"{uid}.mp3")
                subprocess.run(
                    [
                        ffmpeg_bin(),
                        "-y",
                        "-i",
                        native,
                        "-codec:a",
                        "libmp3lame",
                        "-b:a",
                        f"{quality_kbps}k",
                        mp3_path,
                    ],
                    check=True,
                    capture_output=True,
                )
                os.remove(native)
                return mp3_path
            if os.path.splitext(native)[1].lower() == ".webm":
                m4a_path = os.path.join(TEMP_DIR, f"{uid}.m4a")
                try:
                    subprocess.run(
                        [
                            ffmpeg_bin(),
                            "-y",
                            "-i",
                            native,
                            "-codec:a",
                            "aac",
                            "-b:a",
                            "128k",
                            m4a_path,
                        ],
                        check=True,
                        capture_output=True,
                    )
                    os.remove(native)
                    return m4a_path
                except subprocess.CalledProcessError:
                    return native
            return native

        if phase_cb:
            phase_cb("تم تنزيل الصوت (100%). جاري قصّ المقطع بنسخ مباشر سريع (بدون إعادة ترميز)...")
        ext = os.path.splitext(native)[1].lower()
        if ext not in (".m4a", ".webm"):
            ext = ".m4a"
        cut_path = os.path.join(TEMP_DIR, f"{uid}.cut{ext}")
        copy_cmd = [
            ffmpeg_bin(),
            "-y",
            "-ss",
            str(start),
            "-t",
            str(end - start),
            "-i",
            native,
            "-c:a",
            "copy",
            "-map_metadata",
            "-1",
        ]
        if ext == ".m4a":
            copy_cmd += ["-movflags", "+faststart"]
        copy_cmd.append(cut_path)
        try:
            subprocess.run(copy_cmd, check=True, capture_output=True)
        except subprocess.CalledProcessError:
            if phase_cb:
                phase_cb("تعذّر القصّ بالنسخ المباشر، جاري التحويل إلى M4A...")
            cut_path = os.path.join(TEMP_DIR, f"{uid}.cut.m4a")
            subprocess.run(
                [
                    ffmpeg_bin(),
                    "-y",
                    "-ss",
                    str(start),
                    "-t",
                    str(end - start),
                    "-i",
                    native,
                    "-codec:a",
                    "aac",
                    "-b:a",
                    "128k",
                    cut_path,
                ],
                check=True,
                capture_output=True,
            )

        final = cut_path
        if os.path.getsize(final) > MAX_UPLOAD_BYTES:
            if phase_cb:
                phase_cb("حجم المقطع كبير، جاري ضغطه إلى MP3 بالجودة المختارة...")
            mp3_path = os.path.join(TEMP_DIR, f"{uid}.mp3")
            subprocess.run(
                [
                    ffmpeg_bin(),
                    "-y",
                    "-i",
                    final,
                    "-codec:a",
                    "libmp3lame",
                    "-b:a",
                    f"{quality_kbps}k",
                    mp3_path,
                ],
                check=True,
                capture_output=True,
            )
            os.remove(final)
            final = mp3_path

        os.remove(native)
        return final

    return await asyncio.to_thread(_run)


def cleanup(uid: str) -> None:
    for path in glob.glob(os.path.join(TEMP_DIR, f"{uid}.*")):
        try:
            os.remove(path)
        except OSError:
            logger.warning("تعذر حذف الملف المؤقت: %s", path)


def build_quick_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("جودة عالية (192)", callback_data="q192"),
            InlineKeyboardButton("متوسطة (128)", callback_data="q128"),
            InlineKeyboardButton("منخفضة (96)", callback_data="q96"),
            InlineKeyboardButton("الأقل (64)", callback_data="q64"),
        ],
        [
            InlineKeyboardButton("أول 30 ثانية", callback_data="first30"),
            InlineKeyboardButton("أول دقيقة", callback_data="first60"),
        ],
        [
            InlineKeyboardButton("الصوت الكامل", callback_data="full"),
            InlineKeyboardButton("إلغاء", callback_data="cancel"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if ALLOWED_USER_IDS and update.effective_user.id not in ALLOWED_USER_IDS:
        await update.message.reply_text("عذراً، هذا البوت غير متاح لك.")
        return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text(
        "مرحباً بك في بوت قصّ الصوتيات من يوتيوب.\n\n"
        "أرسل لي رابط فيديو يوتيوب لأرسل لك صوته بالشكل الذي تريده:\n"
        "- قصّ جزء محدد بالتوقيت\n"
        "- أو الصوت كاملاً\n"
        "أو أرسل /help لمعرفة كل الخيارات."
    )
    return WAITING_FOR_URL


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "كيفية الاستخدام:\n\n"
        "1. أرسل رابط فيديو يوتيوب.\n"
        "2. بعد ظهور معلومات الفيديو، أرسل التوقيت بهذا الشكل:\n"
        "   MM:SS - MM:SS   (مثال: 01:30 - 04:15)\n"
        "   HH:MM:SS - HH:MM:SS   (مثال: 00:01:30 - 00:04:15)\n"
        "3. أو استخدم الأزرار السريعة (أول 30 ثانية / أول دقيقة / الصوت الكامل).\n"
        "4. أو اكتب كلمة: كامل  لإرسال صوت الفيديو كاملاً.\n\n"
        "الأوامر:\n"
        "/start - بدء محادثة جديدة\n"
        "/cancel - إلغاء العملية الحالية"
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("تم إلغاء العملية. أرسل /start للبدء من جديد.")
    context.user_data.clear()
    return ConversationHandler.END


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    match = YOUTUBE_URL_RE.search(text)
    if not match:
        await update.message.reply_text(
            "الرابط الذي أرسلته ليس رابط يوتيوب صحيح.\n"
            "أرسل رابطاً من YouTube أو youtu.be من فضلك."
        )
        return WAITING_FOR_URL

    video_id = match.group(1)
    context.user_data["url"] = f"https://www.youtube.com/watch?v={video_id}"

    status = await update.message.reply_text("جاري جلب معلومات الفيديو...")
    try:
        info = await get_video_info(context.user_data["url"])
    except Exception as exc:
        logger.exception("فشل جلب معلومات الفيديو (handle_url): %s", context.user_data["url"])
        await safe_edit(status, arabic_error(exc))
        return WAITING_FOR_URL

    title = (info.get("title") or "مقطع صوتي").strip()
    duration = info.get("duration") or 0
    channel = (info.get("channel") or "").strip()

    context.user_data["title"] = title
    context.user_data["duration"] = duration
    context.user_data["video_info"] = info

    text = (
        f"تم التعرف على الفيديو:\n\n"
        f"العنوان: {title}\n"
        f"القناة: {channel or 'غير معروفة'}\n"
        f"المدة: {format_time(duration)}\n\n"
        f"أرسل توقيت القص بالشكل:\n"
        f"MM:SS - MM:SS   (مثال: 01:30 - 04:15)\n"
        f"HH:MM:SS - HH:MM:SS   (مثال: 00:01:30 - 00:04:15)\n"
        f"أو استخدم الأزرار أدناه، أو اكتب: كامل"
    )
    await status.edit_text(text, reply_markup=build_quick_keyboard())
    return WAITING_FOR_TIME


async def handle_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()

    if YOUTUBE_URL_RE.search(text):
        return await handle_url(update, context)

    if text.replace(" ", "").lower() in ("كامل", "كل", "full", "all"):
        context.user_data.pop("start", None)
        context.user_data.pop("end", None)
        context.user_data["full"] = True
        await start_processing(update, context)
        return ConversationHandler.END

    tokens = [token for token in re.split(r"[\s\-–—,]+", text) if token]
    if len(tokens) != 2:
        await update.message.reply_text(
            "لم أفهم التوقيت. أرسله بهذا الشكل:\nمثال: 01:30 - 04:15"
        )
        return WAITING_FOR_TIME

    start, end = parse_time(tokens[0]), parse_time(tokens[1])
    if start is None or end is None:
        await update.message.reply_text(
            "صيغة الأوقات غير صحيحة. استخدم MM:SS أو HH:MM:SS\nمثال: 01:30 - 04:15"
        )
        return WAITING_FOR_TIME

    if end <= start:
        await update.message.reply_text(
            "وقت النهاية يجب أن يكون أكبر من وقت البداية.\nأعد الإرسال بالشكل الصحيح."
        )
        return WAITING_FOR_TIME

    context.user_data["start"] = start
    context.user_data["end"] = end
    context.user_data.pop("full", None)

    await start_processing(update, context)
    return ConversationHandler.END


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "cancel":
        await query.edit_message_text("تم إلغاء العملية. أرسل /start للبدء من جديد.")
        context.user_data.clear()
        return ConversationHandler.END

    if data == "full":
        context.user_data.pop("start", None)
        context.user_data.pop("end", None)
        context.user_data["full"] = True
    elif data == "first30":
        context.user_data.update(start=0, end=30, full=False)
    elif data == "first60":
        context.user_data.update(start=0, end=60, full=False)
    elif data in ("q192", "q128", "q96", "q64"):
        quality = data[1:]
        context.user_data["quality"] = quality
        await query.edit_message_text(
            f"تم اختيار الجودة: {quality}kbps\n\n"
            "الآن أرسل التوقيت (مثال: 01:30 - 04:15) أو استخدم الأزرار.",
            reply_markup=build_quick_keyboard(),
        )
        return WAITING_FOR_TIME

    await start_processing(update, context)
    return ConversationHandler.END


async def start_processing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.callback_query:
        status = await update.callback_query.message.reply_text(
            "جاري معالجة الصوت... يرجى الانتظار."
        )
    else:
        status = await update.message.reply_text(
            "جاري معالجة الصوت... يرجى الانتظار."
        )
    try:
        await process(context, status)
    finally:
        context.user_data.clear()


async def process(context: ContextTypes.DEFAULT_TYPE, status) -> None:
    url = context.user_data["url"]
    full = context.user_data.pop("full", False)
    start = context.user_data.get("start")
    end = context.user_data.get("end")

    info = context.user_data.pop("video_info", None)
    if info is None:
        try:
            info = await get_video_info(url)
        except Exception as exc:
            logger.exception("فشل جلب معلومات الفيديو")
            await safe_edit(status, arabic_error(exc))
            return

    duration = info.get("duration") or 0
    title = (info.get("title") or "مقطع صوتي").strip()
    quality_kbps = int(context.user_data.get("quality", AUDIO_QUALITY))

    if not full:
        if duration and end > duration:
            await safe_edit(
                status,
                f"وقت النهاية ({format_time(end)}) يتجاوز مدة الفيديو ({format_time(duration)}).\n"
                "أرسل /start وجرّب توقيتاً صحيحاً.",
            )
            return
        if end - start > MAX_SEGMENT_SECONDS:
            await safe_edit(
                status,
                f"المقطع المطلوب أطول من المسموح (الحد الأقصى {MAX_SEGMENT_SECONDS // 3600} ساعة "
                f"و{(MAX_SEGMENT_SECONDS % 3600) // 60} دقيقة).\n"
                "أرسل /start وجرّب مقطعاً أقصر.",
            )
            return
        segment_seconds = end - start
    else:
        segment_seconds = duration

    if not full:
        estimated_size = estimate_mp3_size(segment_seconds, quality_kbps)
        if estimated_size > MAX_UPLOAD_BYTES:
            estimated_mb = estimated_size / (1024 * 1024)
            await safe_edit(
                status,
                f"المدة المطلوبة ({format_time(segment_seconds)}) ستنتج ملفاً بحجم تقريبي "
                f"{estimated_mb:.0f}MB (بجودة {quality_kbps}kbps)، وهو أكبر من حد إرسال تيليغرام (50MB).\n"
                "جرّب جودة أقل، أو مدة أقصر، أو قسم الفيديو إلى عدة مقاطع.",
            )
            return

    uid = uuid.uuid4().hex
    loop = asyncio.get_running_loop()
    try:
        if full:
            segment_label = "كامل"
            await safe_edit(
                status, f"العنوان: {title}\nجاري تنزيل الصوت كاملاً..."
            )
        else:
            segment_label = f"{format_time(start)} - {format_time(end)}"
            await safe_edit(
                status,
                f"العنوان: {title}\nجاري تنزيل الصوت وقصّه ({segment_label})...",
            )

        progress_hook = build_progress_hook(status, loop)
        phase_cb = build_phase_editor(status, loop)
        final_path = await download_audio(
            url,
            uid,
            None if full else start,
            None if full else end,
            progress_hook=progress_hook,
            quality_kbps=quality_kbps,
            phase_cb=phase_cb,
            info=info,
        )
        if not final_path:
            raise RuntimeError("لم يتم إنشاء ملف الصوت النهائي.")

        file_size = os.path.getsize(final_path)
        if file_size > MAX_UPLOAD_BYTES:
            await safe_edit(
                status,
                "حجم الملف الناتج يتجاوز الحد المسموح للإرسال في تيليغرام (50MB) "
                "حتى بعد ضغط الجودة. جرّب مقطعاً أقصر.",
            )
            return

        await safe_edit(status, "جاري إرسال الملف...")
        caption = f"{title}\nالتوقيت: {segment_label}"
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with open(final_path, "rb") as audio_file:
                    await status.reply_audio(
                        audio=audio_file,
                        title=title,
                        caption=caption,
                        performer="YouTube",
                    )
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                logger.warning("فشل إرسال الملف (محاولة %d): %s", attempt + 1, exc)
                await asyncio.sleep(5 * (attempt + 1))
        if last_error is not None:
            raise last_error
        try:
            await status.delete()
        except Exception:
            pass
    except Exception as exc:
        logger.exception("فشل معالجة المقطع")
        await safe_edit(status, arabic_error(exc))
    finally:
        cleanup(uid)


async def prompt_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("أرسل /start لبدء استخدام البوت.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("خطأ غير متوقع أثناء معالجة التحديث:", exc_info=context.error)


def build_application() -> Application:
    builder = (
        Application.builder()
        .token(BOT_TOKEN)
        .read_timeout(300)
        .write_timeout(300)
        .media_write_timeout(600)
        .connect_timeout(20)
    )
    if BOT_API_URL != "https://api.telegram.org/bot":
        builder = builder.base_url(BOT_API_URL).base_file_url(BOT_API_FILE_URL)
    application = builder.build()

    conversation_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAITING_FOR_URL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url)
            ],
            WAITING_FOR_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_time),
                CallbackQueryHandler(handle_callback),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
        ],
    )

    application.add_handler(conversation_handler)
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, prompt_start))
    application.add_error_handler(error_handler)
    return application


def run_webhook_mode(application: Application | None) -> None:
    import uvicorn
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, Response

    web_app = FastAPI()

    @web_app.on_event("startup")
    async def _startup():
        if application is None:
            logger.error("لا يوجد تطبيق بوت لتفعيل webhook.")
            return
        await application.initialize()
        await application.start()
        webhook_secret = WEBHOOK_SECRET or None
        await application.bot.set_webhook(
            url=WEBHOOK_URL + WEBHOOK_PATH,
            secret_token=webhook_secret,
            drop_pending_updates=True,
        )
        logger.info("تم تفعيل الـ webhook على %s%s", WEBHOOK_URL, WEBHOOK_PATH)

    @web_app.on_event("shutdown")
    async def _shutdown():
        if application is not None:
            await application.stop()
            await application.shutdown()

    @web_app.get("/healthz")
    async def _healthz():
        return JSONResponse({"status": "ok"})

    @web_app.get("/diag")
    async def _diag():
        import asyncio
        results = {}
        cookie_ok = False
        try:
            cookie_ok = os.path.isfile(COOKIES_FILE) and "SID" in open(COOKIES_FILE).read()
        except Exception:
            pass
        results["cookies_sid"] = str(cookie_ok)
        clients = {
            "tv": (["tv"], True),
        }
        for label, url in [
            ("normal_video", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
            ("age_gated", "https://www.youtube.com/watch?v=rupFLbOkioQ"),
        ]:
            for cname, (c, use_cookies) in clients.items():
                def _try(url=url, c=c, use_cookies=use_cookies):
                    opts = base_ytdlp_options(use_cookies=use_cookies)
                    opts.update({
                        "quiet": True,
                        "noplaylist": True,
                        "skip_download": True,
                        "extractor_args": {"youtube": {"player_client": c}},
                    })
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        return ydl.extract_info(url, download=False)
                try:
                    info = await asyncio.wait_for(asyncio.to_thread(_try), timeout=25)
                    fmts = info.get("formats") or []
                    results[f"{cname}@{label}"] = f"OK fmts={len(fmts)} url={sum(1 for f in fmts if f.get('url'))}"
                except Exception as exc:
                    results[f"{cname}@{label}"] = f"FAIL {str(exc)[:90]}"
        return JSONResponse(results)

    @web_app.post(WEBHOOK_PATH)
    async def _webhook(request: Request):
        if application is None:
            return Response(status_code=503)
        if WEBHOOK_SECRET and (
            request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET
        ):
            return Response(status_code=401)
        data = json.loads(await request.body())
        update = Update.de_json(data, application.bot)
        await application.update_queue.put(update)
        return Response(status_code=200)

    uvicorn.run(web_app, host="0.0.0.0", port=PORT, log_level="info")


def run_polling_with_health(application: Application | None) -> None:
    import asyncio
    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    web_app = FastAPI()

    async def _start_bot() -> None:
        if application is None:
            logger.error("لا يوجد تطبيق بوت لبدء polling.")
            return
        try:
            await application.initialize()
            await application.start()
            await application.updater.start_polling(drop_pending_updates=True)
            logger.info("تم تشغيل البوت بوضع polling على منفذ %s", PORT)
        except Exception:
            logger.exception("فشل تشغيل البوت بوضع polling")

    @web_app.on_event("startup")
    async def _startup():
        asyncio.create_task(_start_bot())

    @web_app.on_event("shutdown")
    async def _shutdown():
        if application is not None:
            await application.stop()
            await application.shutdown()

    @web_app.get("/healthz")
    async def _healthz():
        return JSONResponse({"status": "ok"})

    uvicorn.run(web_app, host="0.0.0.0", port=PORT, log_level="info")


def main() -> None:
    if not BOT_TOKEN:
        logger.error(
            "لم يتم تحديد BOT_TOKEN. اضبط متغير البيئة BOT_TOKEN أولاً ثم أعد التشغيل."
        )

    try:
        application = build_application()
    except Exception as exc:
        logger.error("فشل بناء التطبيق: %s", exc)
        application = None

    if WEBHOOK_URL:
        run_webhook_mode(application)
    else:
        run_polling_with_health(application)


if __name__ == "__main__":
    main()
