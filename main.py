import asyncio
import glob
import json
import logging
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import yt_dlp

# ---------------------------------------------------------------------------
# الإعدادات من متغيرات البيئة
# ---------------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ALLOWED_USER_IDS = {
    int(uid)
    for uid in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if uid.strip()
}
MAX_SEGMENT_SECONDS = int(os.getenv("MAX_SEGMENT_SECONDS", "7200"))
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))
PROCESS_TIMEOUT = int(os.getenv("PROCESS_TIMEOUT", "180"))
TEMP_DIR = os.environ.get("TEMP_DIR") or tempfile.gettempdir()
PORT = int(os.getenv("PORT", "8080"))

# خيارات الجودة المتاحة
QUALITY_OPTIONS = {
    "high": {"label": "عالية (أفضل جودة)", "bitrate": None},
    "medium": {"label": "متوسطة (128k)", "bitrate": 128},
    "low": {"label": "منخفضة (64k)", "bitrate": 64},
}
DEFAULT_QUALITY = "high"

# ---------------------------------------------------------------------------
# سجل الأحداث
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# مسارات وأدوات النظام
# ---------------------------------------------------------------------------
FFMPEG = os.getenv("FFMPEG_LOCATION", "ffmpeg")

# ---------------------------------------------------------------------------
# إعدادات yt-dlp المصممة لتجاوز حجب مراكز البيانات قدر الإمكان
# ---------------------------------------------------------------------------
# استخدام عدة عملاء مختلفي المنصات بالتناوب: tv، ios، android_vr، web_safari.
# عند فشل أحدهم ينتقل تلقائياً للتالي. تجربة عملاء أندرويد/iOS عادةً تتفادى
# فحص المتصفح الذي يطبقه YouTube على العملاء الويب.
YDL_COMMON = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "skip_download": False,
    "retries": 5,
    "fragment_retries": 5,
    "socket_timeout": 60,
    "concurrent_fragment_downloads": 10,
    "extractor_args": {
        "youtube": {
            "player_client": [
                "tv",
                "ios",
                "android_vr",
                "web_safari",
                "web_embedded",
            ],
        }
    },
}

# بيئة تشغيل JavaScript (Node.js) لحل تحديات توقيع nsig
YDL_COMMON["js_runtimes"] = {"node": {}}

# ---------------------------------------------------------------------------
# أدوات مساعدة
# ---------------------------------------------------------------------------
YOUTUBE_URL_RE = re.compile(
    r"(?:youtube\.com\/(?:watch\?(?:.*&)?v=|shorts\/|embed\/|live\/)|youtu\.be\/)"
    r"([A-Za-z0-9_-]{11})"
)
TIME_RE = re.compile(r"^(?:(\d+):)?([0-5]?\d):([0-5]\d)$")


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
    if "sign in to confirm" in message:
        return (
            "يوتيوب يطلب تأكيد هوية بشرية (رobot check). "
            "يحدث هذا غالباً عندما يكون IP الخادم محظوراً من يوتيوب."
        )
    if "requested format" in message or "format is not available" in message:
        return "الصيغة المطلوبة غير متاحة لهذا الفيديو."
    if "private" in message or "members-only" in message:
        return "هذا الفيديو خاص أو للأعضاء فقط."
    if "video unavailable" in message or "removed" in message:
        return "هذا الفيديو غير متاح (محذوف أو محظور في بلدك)."
    if "age-restricted" in message:
        return "هذا الفيديو مقيد بالعمر."
    if "copyright" in message:
        return "تمت إزالة هذا الفيديو بسبب حقوق النشر."
    if "live stream" in message or "is live" in message:
        return "لا يمكن قصّ البث المباشر بهذه الطريقة."
    if "timeout" in message or "timed out" in message:
        return "انتهت مهلة الاتصال. حاول مرة أخرى."
    return f"حدث خطأ: {underlying_error(exc)[:200]}"


def ffmpeg_bin() -> str:
    if os.path.isdir(FFMPEG):
        return os.path.join(FFMPEG, "ffmpeg")
    return FFMPEG


def cleanup(uid: str) -> None:
    for path in glob.glob(os.path.join(TEMP_DIR, f"{uid}.*")):
        try:
            os.remove(path)
        except OSError:
            pass


def make_progress_hook(progress_q: "queue.Queue"):
    """خطاف تقدم التنزيل: ينقل نسبة التقدم من خيط yt-dlp إلى قائمة آمنة"""
    def hook(data: dict):
        try:
            if data.get("status") == "downloading":
                done = data.get("downloaded_bytes") or 0
                total = (
                    data.get("total_bytes")
                    or data.get("total_bytes_estimate")
                    or 0
                )
                progress_q.put(("download", done, total))
        except Exception:
            pass
    return hook


# ---------------------------------------------------------------------------
# جلب معلومات الفيديو مع تناوب العملاء
# ---------------------------------------------------------------------------
async def get_video_info(url: str) -> dict:
    def _fetch() -> dict:
        last_error: Exception | None = None
        client_sets = [
            ["android_vr"],
            ["ios"],
            ["tv"],
        ]
        for clients in client_sets:
            opts = dict(YDL_COMMON)
            opts["extractor_args"] = {
                "youtube": {"player_client": clients}
            }
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    return ydl.extract_info(url, download=False)
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "فشل جلب المعلومات بالعملاء %s: %s",
                    clients, underlying_error(exc),
                )
        raise last_error or RuntimeError("فشل جلب معلومات الفيديو")

    return await asyncio.to_thread(_fetch)


# ---------------------------------------------------------------------------
# تنزيل + قصّ الصوت بدون إعادة ترميز (قصّ لحظي سريع)
# ---------------------------------------------------------------------------
async def download_and_cut(
    url: str,
    uid: str,
    start: int | None,
    end: int | None,
    quality: str = DEFAULT_QUALITY,
    hook: "queue.Queue | None" = None,
) -> str | None:
    """تنزيل أفضل صوت ثم إنتاجه بالجودة المطلوبة.

    - جودة عالية: قصّ بنسخ مباشر -c:a copy (بدون إعادة ترميز، فوري).
    - جودة أقل: تفضيل صيغة أقل معدل بتاً للتنزيل، وإن لزم إعادة ترميز AAC
      بمعدل البت المطلوب لضمان الحجم الصغير.
    """
    bitrate = QUALITY_OPTIONS[quality]["bitrate"]
    is_segment = start is not None and end is not None

    def _run() -> str | None:
        outtmpl = os.path.join(TEMP_DIR, f"{uid}.%(ext)s")

        # للقصّ: ننزّل الجزء المطلوب فقط عبر download_ranges (بدل الصوت كاملاً).
        # هذا يقلص حجم التنزيل عشرات المرات (مثلاً 159KB بدل 21MB) فينجز بسرعة
        # حتى على شبكات بطيئة. نفضّل m4a لأن تدفقه متسلسل فينزّل بسرعة موثوقة.
        last_error: Exception | None = None
        for attempt in range(4):
            opts = dict(YDL_COMMON)

            # نثبّت android_vr للتنزيل: قائمة العملاء الكاملة مع download=True قد تعلق
            opts["extractor_args"] = {"youtube": {"player_client": ["android_vr"]}}

            if bitrate:
                fmt = (
                    f"bestaudio[abr<={bitrate}][ext=m4a]/"
                    f"bestaudio[abr<={bitrate}]/"
                    f"bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best"
                )
            else:
                fmt = "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best"
            opts.update({"format": fmt, "outtmpl": outtmpl})

            # للمقاطع فقط: ننزّل الجزء المطلوب دون إعادة ترميز
            if is_segment:
                opts["download_ranges"] = lambda info, ydl: [
                    {"start_time": start, "end_time": end}
                ]
            if hook is not None:
                opts["progress_hooks"] = [make_progress_hook(hook)]

            try:
                for leftover in glob.glob(os.path.join(TEMP_DIR, f"{uid}.*")):
                    try:
                        os.remove(leftover)
                    except OSError:
                        pass
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.extract_info(url, download=True)
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "فشل التنزيل (محاولة %d): %s",
                    attempt + 1, underlying_error(exc),
                )
                time.sleep(3 * (attempt + 1))

        # الملف الذي أنزله yt-dlp
        matches = sorted(glob.glob(os.path.join(TEMP_DIR, f"{uid}.*")))
        native = next((p for p in matches if not p.endswith(".part")), None)
        if native is None and last_error:
            raise last_error
        return native

    native = await asyncio.to_thread(_run)
    if not native:
        return None

    reencode = bool(bitrate) and _needs_reencode(native, bitrate)

    # إن كان المطلوب الصوت كاملاً: نعيد الملف كما هو إن كان m4a،
    # وإن كان webm نحوّله إلى m4a دون إعادة ترميز إن أمكن (نسخ الحاوية).
    if start is None and end is None and not reencode:
        if native.lower().endswith(".m4a"):
            return native
        if native.lower().endswith(".webm") or native.lower().endswith(".opus"):
            out_path = os.path.join(TEMP_DIR, f"{uid}.m4a")
            try:
                subprocess.run(
                    [
                        ffmpeg_bin(),
                        "-y",
                        "-i",
                        native,
                        "-vn",
                        "-c:a",
                        "copy",
                        out_path,
                    ],
                    check=True,
                    capture_output=True,
                )
                os.remove(native)
                return out_path
            except subprocess.CalledProcessError:
                return native

    # عند الحاجة لجودة أدق من الصيغة المتاحة: إعادة ترميز بمعدل البت المطلوب
    if reencode:
        # مع download_ranges يبدأ الملف الجزئي من 0، فالتشفير يكون من البداية
        encode_from = 0 if is_segment else start
        return _encode_aac(native, uid, bitrate, encode_from, end)

    # قصّ مقطع: نسخ مباشر -c:a copy (فوري، بلا إعادة ترميز)
    # (مع download_ranges يبدأ الملف الجزئي من 0، فالقصّ يكون من البداية)
    out_path = os.path.join(TEMP_DIR, f"{uid}-cut.m4a")
    duration = end - start
    seek_at = 0 if is_segment else start
    subprocess.run(
        [
            ffmpeg_bin(),
            "-y",
            "-ss",
            str(seek_at),
            "-t",
            str(duration),
            "-i",
            native,
            "-vn",
            "-c:a",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
            out_path,
        ],
        check=True,
        capture_output=True,
    )
    os.remove(native)
    return out_path


def _needs_reencode(path: str, bitrate: int) -> bool:
    """يرجع True إن كانت الصيغة أعلى من معدل البت المطلوب (فتحتاج ترميزاً أدق)."""
    try:
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=bit_rate:stream=codec_name,bit_rate",
                "-of",
                "json",
                path,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        data = json.loads(probe.stdout)
        codec = None
        stream_bitrate = 0
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "audio":
                codec = stream.get("codec_name")
                stream_bitrate = int(stream.get("bit_rate") or 0)
                break
        if codec and codec not in ("aac", "mp4a", "opus"):
            return True
        effective = stream_bitrate or int(data.get("format", {}).get("bit_rate") or 0)
        return effective > bitrate * 1000 * 1.15
    except Exception:
        return True


def _encode_aac(
    native: str,
    uid: str,
    bitrate: int,
    start: int | None,
    end: int | None,
) -> str:
    """إعادة ترميز AAC بمعدل بت محدد (لجودة أقل)."""
    out_path = os.path.join(TEMP_DIR, f"{uid}-q.m4a")
    cmd = [ffmpeg_bin(), "-y"]
    if start is not None and end is not None:
        cmd += ["-ss", str(start), "-t", str(end - start)]
    cmd += ["-i", native, "-vn", "-c:a", "aac", "-b:a", f"{bitrate}k", out_path]
    subprocess.run(cmd, check=True, capture_output=True)
    os.remove(native)
    return out_path


# ---------------------------------------------------------------------------
# واجهة Telegram Bot API عبر مكتبة Python القياسية فقط (لا حاجة لبناء أي شيء)
# ---------------------------------------------------------------------------
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"


def _http_json(url: str, data: dict | None = None, files: dict | None = None,
               timeout: int = 90) -> dict:
    if files:
        boundary = uuid.uuid4().hex
        body = bytearray()
        for key, value in (data or {}).items():
            body += (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{value}\r\n"
            ).encode()
        for key, (filename, content) in files.items():
            body += (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{key}"; '
                f'filename="{filename}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n"
            ).encode()
            body += content
            body += b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        request = urllib.request.Request(
            url,
            data=bytes(body),
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
    else:
        payload = urllib.parse.urlencode(data or {}).encode()
        request = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        description = str(exc)
        try:
            body = json.loads(exc.read().decode())
            description = body.get("description") or description
        except Exception:
            pass
        raise RuntimeError(description)


def tg(method: str, _timeout: int = 90, **params) -> object:
    data = _http_json(f"{API_BASE}/{method}", data=params, timeout=_timeout)
    if not data.get("ok"):
        raise RuntimeError(data.get("description") or "خطأ في Telegram API")
    return data.get("result")


def tg_audio(method: str, path: str, fields: dict, timeout: int = 300) -> object:
    with open(path, "rb") as handle:
        content = handle.read()
    data = _http_json(
        f"{API_BASE}/{method}",
        data=fields,
        files={"audio": (os.path.basename(path), content)},
        timeout=timeout,
    )
    if not data.get("ok"):
        raise RuntimeError(data.get("description") or "خطأ في إرسال الصوت")
    return data.get("result")


async def send_message(chat_id: int, text: str, reply_markup: dict | None = None) -> dict:
    params = {"chat_id": chat_id, "text": text}
    if reply_markup:
        params["reply_markup"] = json.dumps(reply_markup)
    result = await asyncio.to_thread(tg, "sendMessage", 90, **params)
    return result or {}


async def edit_message(chat_id: int, message_id: int, text: str,
                       reply_markup: dict | None = None) -> bool:
    params = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if reply_markup:
        params["reply_markup"] = json.dumps(reply_markup)
    try:
        await asyncio.to_thread(tg, "editMessageText", 90, **params)
        return True
    except Exception as exc:
        logger.warning("فشل تعديل الرسالة (%s): %s", message_id, exc)
        return False


async def answer_callback(query_id: str, text: str | None = None) -> None:
    params = {"callback_query_id": query_id}
    if text:
        params["text"] = text
    try:
        await asyncio.to_thread(tg, "answerCallbackQuery", 90, **params)
    except Exception:
        pass


async def delete_message(chat_id: int, message_id: int) -> None:
    try:
        await asyncio.to_thread(
            tg, "deleteMessage", 90, chat_id=chat_id, message_id=message_id
        )
    except Exception:
        pass


async def send_audio(chat_id: int, path: str, title: str, caption: str,
                     performer: str, filename: str) -> None:
    fields = {
        "chat_id": chat_id,
        "title": title,
        "caption": caption,
        "performer": performer,
    }
    await asyncio.to_thread(tg_audio, "sendAudio", path, fields, 300)


# ---------------------------------------------------------------------------
# أزرار الاختيار السريع
# ---------------------------------------------------------------------------
def quick_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "أول 30 ثانية", "callback_data": "first30"},
                {"text": "أول دقيقة", "callback_data": "first60"},
                {"text": "الصوت كامل", "callback_data": "full"},
            ],
            [
                {"text": "جودة: عالية", "callback_data": "quality:high"},
                {"text": "جودة: متوسطة 128k", "callback_data": "quality:medium"},
                {"text": "جودة: منخفضة 64k", "callback_data": "quality:low"},
            ],
            [
                {"text": "إلغاء", "callback_data": "cancel"},
            ],
        ]
    }


# ---------------------------------------------------------------------------
# جلسات المحادثة (بسيطة: قاموس لكل دردشة)
# ---------------------------------------------------------------------------
sessions: dict[int, dict] = {}


async def handle_url(chat_id: int, session: dict, text: str) -> None:
    match = YOUTUBE_URL_RE.search(text)
    if not match:
        await send_message(chat_id, "هذا ليس رابط يوتيوب صحيح. حاول مجدداً.")
        return

    video_id = match.group(1)
    url = f"https://www.youtube.com/watch?v={video_id}"
    session["url"] = url
    session["quality"] = DEFAULT_QUALITY

    status = await send_message(chat_id, "جاري جلب معلومات الفيديو...")
    status_id = status.get("message_id")
    try:
        info = await get_video_info(url)
    except Exception as exc:
        logger.exception("فشل جلب معلومات الفيديو")
        await edit_message(chat_id, status_id, arabic_error(exc))
        return

    title = (info.get("title") or "مقطع صوتي").strip()
    duration = info.get("duration") or 0
    channel = (info.get("channel") or "").strip()
    session["title"] = title
    session["duration"] = duration
    session["state"] = "time"

    await edit_message(
        chat_id,
        status_id,
        f"تم التعرف على الفيديو:\n\n"
        f"العنوان: {title}\n"
        f"القناة: {channel or 'غير معروفة'}\n"
        f"المدة: {format_time(duration)}\n\n"
        f"أرسل توقيت القص بالشكل:\n"
        f"MM:SS - MM:SS   (مثال: 01:30 - 04:15)\n"
        f"HH:MM:SS - HH:MM:SS   (مثال: 00:01:30 - 00:04:15)\n"
        f"أو استخدم الأزرار أدناه، أو اكتب: كامل\n\n"
        f"الجودة الحالية: {QUALITY_OPTIONS[DEFAULT_QUALITY]['label']}\n"
        f"اختر جودة مختلفة بزر الجودة، أو اكتب: عالي / متوسط / منخفض",
        reply_markup=quick_keyboard(),
    )


async def handle_time(chat_id: int, session: dict, message: dict, text: str) -> None:
    # إن أرسل رابطاً جديداً: نعيد ضبط الجلسة
    if YOUTUBE_URL_RE.search(text):
        session["state"] = "url"
        await handle_url(chat_id, session, text)
        return

    quality_words = {
        "عالي": "high", "عالية": "high", "عالي جداً": "high",
        "متوسط": "medium", "متوسطة": "medium",
        "منخفض": "low", "منخفضة": "low", "64": "low", "128": "medium",
    }
    clean = text.strip().replace(" ", "")
    if clean in quality_words or clean.lower() in {"high", "medium", "low"}:
        quality = quality_words.get(clean, quality_words.get(clean.lower(), clean.lower()))
        if quality not in QUALITY_OPTIONS:
            await send_message(chat_id, "الجودة غير معروفة. الخيارات: عالي / متوسط / منخفض")
            return
        session["quality"] = quality
        await send_message(
            chat_id,
            f"تم اختيار الجودة: {QUALITY_OPTIONS[quality]['label']}. أرسل التوقيت الآن.",
        )
        return

    if text.replace(" ", "").lower() in ("كامل", "كل", "full", "all"):
        if session.get("processing"):
            await send_message(chat_id, "جاري المعالجة بالفعل، انتظر قليلاً...")
            return
        session["start"] = None
        session["end"] = None
        await process_and_send(chat_id, session, message)
        return

    tokens = [token for token in re.split(r"[\s\-–—,]+", text) if token]
    if len(tokens) != 2:
        await send_message(chat_id, "لم أفهم التوقيت. أرسله بهذا الشكل:\nمثال: 01:30 - 04:15")
        return

    start, end = parse_time(tokens[0]), parse_time(tokens[1])
    if start is None or end is None:
        await send_message(chat_id, "صيغة الأوقات غير صحيحة. استخدم MM:SS أو HH:MM:SS")
        return
    if end <= start:
        await send_message(chat_id, "وقت النهاية يجب أن يكون أكبر من وقت البداية.")
        return

    duration = session.get("duration") or 0
    if duration and end > duration:
        await send_message(
            chat_id,
            f"وقت النهاية ({format_time(end)}) يتجاوز مدة الفيديو ({format_time(duration)}).",
        )
        return
    if end - start > MAX_SEGMENT_SECONDS:
        await send_message(
            chat_id,
            f"المقطع المطلوب أطول من المسموح (الحد الأقصى {MAX_SEGMENT_SECONDS // 3600} ساعة).",
        )
        return

    session["start"] = start
    session["end"] = end
    if session.get("processing"):
        await send_message(chat_id, "جاري المعالجة بالفعل، انتظر قليلاً...")
        return
    await process_and_send(chat_id, session, message)


async def handle_callback(chat_id: int, callback: dict) -> None:
    data = callback.get("data", "")
    query_id = callback.get("id")
    message = callback.get("message") or {}
    message_id = message.get("message_id")
    session = sessions.get(chat_id)

    if data == "cancel":
        if session is not None:
            session.clear()
        await edit_message(chat_id, message_id, "تم إلغاء العملية. أرسل /start للبدء من جديد.")
        return

    if data.startswith("quality:"):
        await answer_callback(query_id)
        quality = data.split(":", 1)[1]
        if quality not in QUALITY_OPTIONS or session is None:
            return
        session["quality"] = quality
        await send_message(chat_id, f"تم اختيار الجودة: {QUALITY_OPTIONS[quality]['label']}.")
        return

    if data in {"first30", "first60", "full"}:
        if session is None or not session.get("url"):
            await send_message(chat_id, "أرسل /start للبدء من جديد.")
            return
        if session.get("processing"):
            await answer_callback(query_id, "جاري المعالجة بالفعل، انتظر قليلاً...")
            return
        if data == "full":
            session["start"] = None
            session["end"] = None
        elif data == "first30":
            session["start"] = 0
            session["end"] = 30
        elif data == "first60":
            session["start"] = 0
            session["end"] = 60
        # نزيل رسالة الأزرار فوراً حتى لا يتم الضغط عليها مرتين
        await delete_message(chat_id, message_id)
        await process_and_send(chat_id, session, message)


async def handle_message(message: dict) -> None:
    chat_id = message.get("chat", {}).get("id")
    if chat_id is None:
        return
    user_id = message.get("from", {}).get("id")
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        await send_message(chat_id, "عذراً، هذا البوت غير متاح لك.")
        return

    text = (message.get("text") or "").strip()
    session = sessions.setdefault(chat_id, {"state": "idle"})

    if text == "/start":
        session.clear()
        session["state"] = "url"
        await send_message(
            chat_id,
            "مرحباً بك في بوت قصّ الصوتيات من يوتيوب.\n\n"
            "أرسل رابط فيديو يوتيوب لأرسل لك صوته بالشكل الذي تريده:\n"
            "- قصّ جزء محدد بالتوقيت\n"
            "- أو الصوت كاملاً",
        )
        return
    if text == "/cancel":
        session.clear()
        session["state"] = "idle"
        await send_message(chat_id, "تم إلغاء العملية. أرسل /start للبدء من جديد.")
        return

    if session.get("state") == "url":
        await handle_url(chat_id, session, text)
    elif session.get("state") == "time":
        await handle_time(chat_id, session, message, text)
    elif YOUTUBE_URL_RE.search(text):
        session["state"] = "url"
        await handle_url(chat_id, session, text)
    else:
        await send_message(chat_id, "أرسل /start للبدء.")


# ---------------------------------------------------------------------------
# المعالجة والإرسال
# ---------------------------------------------------------------------------
async def process_and_send(chat_id: int, session: dict, message: dict) -> None:
    url = session.get("url")
    if not url:
        session.clear()
        await send_message(chat_id, "أرسل /start للبدء من جديد.")
        return

    title = (session.get("title") or "مقطع صوتي").strip()
    start = session.get("start")
    end = session.get("end")
    quality = session.get("quality", DEFAULT_QUALITY)
    if quality not in QUALITY_OPTIONS:
        quality = DEFAULT_QUALITY

    status = await send_message(
        chat_id,
        f"جاري تنزيل الصوت وقصّه (جودة: {QUALITY_OPTIONS[quality]['label']})..."
        if start is not None
        else f"جاري تنزيل الصوت كاملاً (جودة: {QUALITY_OPTIONS[quality]['label']})...",
    )
    status_id = status.get("message_id")

    uid = uuid.uuid4().hex
    session["processing"] = True
    try:
        progress_q: queue.Queue = queue.Queue()
        process_task = asyncio.create_task(
            download_and_cut(url, uid, start, end, quality, progress_q)
        )

        async def progress_loop() -> None:
            last_reported = -1
            while not process_task.done():
                try:
                    kind, done, total = progress_q.get_nowait()
                    if kind == "download" and total:
                        pct = int(done * 100 // total)
                        if pct // 10 > last_reported // 10:
                            last_reported = pct
                            if status_id is None:
                                continue
                            if not await edit_message(chat_id, status_id, f"جاري التنزيل... {pct}%"):
                                return
                except queue.Empty:
                    pass
                await asyncio.sleep(0.3)

        progress_task = asyncio.create_task(progress_loop())
        try:
            path = await asyncio.wait_for(process_task, timeout=PROCESS_TIMEOUT)
        except asyncio.TimeoutError:
            process_task.cancel()
            await edit_message(
                chat_id,
                status_id,
                "انتهت مهلة المعالجة (الشبكة بطيئة أو يوتيوب يعرقل التنزيل).\n"
                "جرّب مقطعاً أقصر أو جودة أقل.",
            )
            return
        finally:
            progress_task.cancel()
        if not path:
            raise RuntimeError("لم يتم إنشاء ملف الصوت.")

        if os.path.getsize(path) > MAX_UPLOAD_BYTES:
            await edit_message(
                chat_id,
                status_id,
                "حجم الملف الناتج يتجاوز حد الإرسال في تيليغرام (50MB).\n"
                "جرّب مقطعاً أقصر.",
            )
            return

        await edit_message(chat_id, status_id, "جاري إرسال الملف...")
        if start is not None:
            segment_label = f"{format_time(start)} - {format_time(end)}"
        else:
            segment_label = "كامل"
        caption = f"{title}\nالتوقيت: {segment_label}"
        filename = f"{title[:40]}_{segment_label}.m4a"

        # إرسال كـ Audio وليس Document
        for attempt in range(3):
            try:
                await send_audio(chat_id, path, title, caption, "YouTube", filename)
                break
            except Exception as exc:
                logger.warning("فشل الإرسال (محاولة %d): %s", attempt + 1, exc)
                if attempt < 2:
                    await asyncio.sleep(5 * (attempt + 1))
                else:
                    raise

        await delete_message(chat_id, status_id)
    except Exception as exc:
        logger.exception("فشل معالجة المقطع")
        await edit_message(chat_id, status_id, arabic_error(exc))
    finally:
        cleanup(uid)
        session.clear()


# ---------------------------------------------------------------------------
# حلقة الاستطلاع (polling)
# ---------------------------------------------------------------------------
async def polling_loop() -> None:
    offset = 0
    while True:
        try:
            updates = await asyncio.to_thread(
                tg, "getUpdates", 100, offset=offset, timeout=50
            )
        except Exception as exc:
            logger.warning("فشل جلب التحديثات: %s", exc)
            await asyncio.sleep(3)
            continue
        for update in updates:
            offset = max(offset, int(update.get("update_id", 0)) + 1)
            try:
                if "message" in update:
                    await handle_message(update["message"])
                elif "callback_query" in update:
                    cq = update["callback_query"]
                    chat_id = (cq.get("message") or {}).get("chat", {}).get("id")
                    if chat_id is not None:
                        await handle_callback(chat_id, cq)
            except Exception as exc:
                logger.exception("خطأ في معالجة التحديث: %s", exc)


# ---------------------------------------------------------------------------
# خادم ويب خفيف /ping لمنع نوم الحاوية (اختياري؛ يتجاهل فشل التشغيل)
# ---------------------------------------------------------------------------
def run_ping_server() -> None:
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            try:
                self.wfile.write(b"ok")
            except Exception:
                pass

        def log_message(self, *args):
            pass

    try:
        httpd = HTTPServer(("0.0.0.0", PORT), Handler)
        httpd.serve_forever()
    except Exception as exc:
        logger.warning("تعذر تشغيل خادم /ping على المنفذ %s: %s", PORT, exc)


# ---------------------------------------------------------------------------
# قفل يمنع تشغيل نسختين من البوت في نفس الوقت
# ---------------------------------------------------------------------------
LOCK_FILE = os.path.join(TEMP_DIR, "audiobot.lock")


def acquire_lock() -> bool:
    """يعيد False إن كان البوت يعمل بالفعل في عملية أخرى."""
    try:
        if os.path.exists(LOCK_FILE):
            try:
                with open(LOCK_FILE) as handle:
                    pid = int(handle.read().strip() or "0")
                if pid > 0:
                    os.kill(pid, 0)
                    return False
            except (ValueError, ProcessLookupError, OSError):
                pass
        with open(LOCK_FILE, "w") as handle:
            handle.write(str(os.getpid()))
        return True
    except Exception:
        return True


def release_lock() -> None:
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# نقطة الدخول
# ---------------------------------------------------------------------------
def main() -> None:
    if not BOT_TOKEN:
        logger.error("لا يوجد BOT_TOKEN. اضبط متغير البيئة.")
        sys.exit(1)

    if not acquire_lock():
        logger.error("البوت يعمل بالفعل في نافذة أخرى!")
        print(
            "\n⚠️  البوت يعمل بالفعل في نافذة Termux أخرى!\n"
            "أغلق النافذة القديمة (Ctrl+C) ثم أعد التشغيل هنا.\n"
        )
        sys.exit(1)

    threading.Thread(target=run_ping_server, daemon=True).start()

    logger.info("بدء تشغيل البوت بوضع polling...")
    try:
        asyncio.run(polling_loop())
    except (KeyboardInterrupt, SystemExit):
        logger.info("تم إيقاف البوت.")
    finally:
        release_lock()


if __name__ == "__main__":
    main()
