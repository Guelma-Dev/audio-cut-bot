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
import time
import uuid

import yt_dlp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    CallbackQuery,
)

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
# نضيف node فقط لأن المتصفحات المتاحة في الحاوية محدودة
YDL_COMMON["js_runtimes"] = {"node": {}}

# ---------------------------------------------------------------------------
# حالات محادثة FSM
# ---------------------------------------------------------------------------
class CutState(StatesGroup):
    waiting_for_url = State()
    waiting_for_time = State()


router = Router()
bot: Bot | None = None


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
    """خطاف تقدم التنزيل: ينقل نسبة التقدم من خيط yt-dlp إلى قائمة آمنة
    تقرأها حلقة asyncio لتحديث رسالة التقدم في تيليجرام."""
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
        # العملاء الذين يعملون موثوقين: android_vr ثم ios ثم tv.
        # (قائمة كاملة في extract_info مع download=True قد تعلق؛ هنا download=False)
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
# أزرار الاختيار السريع
# ---------------------------------------------------------------------------
def quick_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="أول 30 ثانية", callback_data="first30"),
                InlineKeyboardButton(text="أول دقيقة", callback_data="first60"),
                InlineKeyboardButton(text="الصوت كامل", callback_data="full"),
            ],
            [
                InlineKeyboardButton(
                    text="جودة: عالية", callback_data="quality:high"
                ),
                InlineKeyboardButton(
                    text="جودة: متوسطة 128k", callback_data="quality:medium"
                ),
                InlineKeyboardButton(
                    text="جودة: منخفضة 64k", callback_data="quality:low"
                ),
            ],
            [
                InlineKeyboardButton(text="إلغاء", callback_data="cancel"),
            ],
        ]
    )


# ---------------------------------------------------------------------------
# معالجات الأوامر
# ---------------------------------------------------------------------------
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    if ALLOWED_USER_IDS and message.from_user.id not in ALLOWED_USER_IDS:
        await message.answer("عذراً، هذا البوت غير متاح لك.")
        return
    await state.clear()
    await message.answer(
        "مرحباً بك في بوت قصّ الصوتيات من يوتيوب.\n\n"
        "أرسل رابط فيديو يوتيوب لأرسل لك صوته بالشكل الذي تريده:\n"
        "- قصّ جزء محدد بالتوقيت\n"
        "- أو الصوت كاملاً"
    )
    await state.set_state(CutState.waiting_for_url)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("تم إلغاء العملية. أرسل /start للبدء من جديد.")


@router.message(CutState.waiting_for_url, F.text)
async def handle_url(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    match = YOUTUBE_URL_RE.search(text)
    if not match:
        await message.answer("هذا ليس رابط يوتيوب صحيح. حاول مجدداً.")
        return

    video_id = match.group(1)
    url = f"https://www.youtube.com/watch?v={video_id}"
    await state.update_data(url=url, quality=DEFAULT_QUALITY)

    status = await message.answer("جاري جلب معلومات الفيديو...")
    try:
        info = await get_video_info(url)
    except Exception as exc:
        logger.exception("فشل جلب معلومات الفيديو")
        await status.edit_text(arabic_error(exc))
        return

    title = (info.get("title") or "مقطع صوتي").strip()
    duration = info.get("duration") or 0
    channel = (info.get("channel") or "").strip()
    await state.update_data(title=title, duration=duration)

    await status.edit_text(
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
    await state.set_state(CutState.waiting_for_time)


@router.callback_query(F.data == "cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    try:
        await callback.message.edit_text("تم إلغاء العملية. أرسل /start للبدء من جديد.")
    except Exception:
        pass


async def safe_answer(callback: CallbackQuery, text: str | None = None) -> None:
    try:
        await callback.answer(text)
    except Exception:
        pass


@router.callback_query(F.data.startswith("quality:"))
async def cb_quality(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    quality = callback.data.split(":", 1)[1]
    if quality not in QUALITY_OPTIONS:
        return
    await state.update_data(quality=quality)
    label = QUALITY_OPTIONS[quality]["label"]
    await callback.message.answer(f"تم اختيار الجودة: {label}.")


@router.callback_query(F.data.in_({"first30", "first60", "full"}))
async def cb_quick(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    data = await state.get_data()
    if data.get("url") is None:
        await state.clear()
        await callback.message.answer("أرسل /start للبدء من جديد.")
        return

    if callback.data == "full":
        await state.update_data(start=None, end=None)
    elif callback.data == "first30":
        await state.update_data(start=0, end=30)
    elif callback.data == "first60":
        await state.update_data(start=0, end=60)

    try:
        await callback.message.edit_text("جاري المعالجة...")
    except Exception:
        pass
    await process_and_send(callback.message, state)


@router.message(CutState.waiting_for_time, F.text)
async def handle_time(message: Message, state: FSMContext) -> None:
    text = message.text.strip()

    # إن أرسل رابطاً جديداً: نعيد ضبط الجلسة
    if YOUTUBE_URL_RE.search(text):
        await handle_url(message, state)
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
            await message.answer("الجودة غير معروفة. الخيارات: عالي / متوسط / منخفض")
            return
        await state.update_data(quality=quality)
        await message.answer(
            f"تم اختيار الجودة: {QUALITY_OPTIONS[quality]['label']}. أرسل التوقيت الآن."
        )
        return

    if text.replace(" ", "").lower() in ("كامل", "كل", "full", "all"):
        await state.update_data(start=None, end=None)
        await process_and_send(message, state)
        return

    tokens = [token for token in re.split(r"[\s\-–—,]+", text) if token]
    if len(tokens) != 2:
        await message.answer("لم أفهم التوقيت. أرسله بهذا الشكل:\nمثال: 01:30 - 04:15")
        return

    start, end = parse_time(tokens[0]), parse_time(tokens[1])
    if start is None or end is None:
        await message.answer("صيغة الأوقات غير صحيحة. استخدم MM:SS أو HH:MM:SS")
        return
    if end <= start:
        await message.answer("وقت النهاية يجب أن يكون أكبر من وقت البداية.")
        return

    data = await state.get_data()
    duration = data.get("duration") or 0
    if duration and end > duration:
        await message.answer(
            f"وقت النهاية ({format_time(end)}) يتجاوز مدة الفيديو ({format_time(duration)})."
        )
        return
    if end - start > MAX_SEGMENT_SECONDS:
        await message.answer(
            f"المقطع المطلوب أطول من المسموح (الحد الأقصى {MAX_SEGMENT_SECONDS // 3600} ساعة)."
        )
        return

    await state.update_data(start=start, end=end)
    await process_and_send(message, state)


# ---------------------------------------------------------------------------
# المعالجة والإرسال
# ---------------------------------------------------------------------------
async def process_and_send(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    url = data.get("url")
    if not url:
        await state.clear()
        await message.answer("أرسل /start للبدء من جديد.")
        return

    title = (data.get("title") or "مقطع صوتي").strip()
    start = data.get("start")
    end = data.get("end")
    quality = data.get("quality", DEFAULT_QUALITY)
    if quality not in QUALITY_OPTIONS:
        quality = DEFAULT_QUALITY

    status = await message.answer(
        f"جاري تنزيل الصوت وقصّه (جودة: {QUALITY_OPTIONS[quality]['label']})..."
        if start is not None
        else f"جاري تنزيل الصوت كاملاً (جودة: {QUALITY_OPTIONS[quality]['label']})..."
    )

    uid = uuid.uuid4().hex
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
                            try:
                                await status.edit_text(
                                    f"جاري التنزيل... {pct}%"
                                )
                            except Exception:
                                pass
                except queue.Empty:
                    pass
                await asyncio.sleep(0.3)

        progress_task = asyncio.create_task(progress_loop())
        try:
            path = await asyncio.wait_for(process_task, timeout=PROCESS_TIMEOUT)
        except asyncio.TimeoutError:
            process_task.cancel()
            await status.edit_text(
                "انتهت مهلة المعالجة (الشبكة بطيئة أو يوتيوب يعرقل التنزيل).\n"
                "جرّب مقطعاً أقصر أو جودة أقل."
            )
            return
        finally:
            progress_task.cancel()
        if not path:
            raise RuntimeError("لم يتم إنشاء ملف الصوت.")

        if os.path.getsize(path) > MAX_UPLOAD_BYTES:
            await status.edit_text(
                "حجم الملف الناتج يتجاوز حد الإرسال في تيليغرام (50MB).\n"
                "جرّب مقطعاً أقصر."
            )
            return

        await status.edit_text("جاري إرسال الملف...")
        if start is not None:
            segment_label = f"{format_time(start)} - {format_time(end)}"
        else:
            segment_label = "كامل"
        caption = f"{title}\nالتوقيت: {segment_label}"

        # إرسال كـ Audio وليس Document
        for attempt in range(3):
            try:
                with open(path, "rb") as audio_file:
                    await message.answer_audio(
                        audio=BufferedInputFile(audio_file.read(), filename=f"{title[:40]}_{segment_label}.m4a"),
                        title=title,
                        caption=caption,
                        performer="YouTube",
                    )
                break
            except Exception as exc:
                logger.warning("فشل الإرسال (محاولة %d): %s", attempt + 1, exc)
                if attempt < 2:
                    await asyncio.sleep(5 * (attempt + 1))
                else:
                    raise

        try:
            await status.delete()
        except Exception:
            pass
    except Exception as exc:
        logger.exception("فشل معالجة المقطع")
        await status.edit_text(arabic_error(exc))
    finally:
        cleanup(uid)
        await state.clear()


# ---------------------------------------------------------------------------
# خادم ويب خفيف /ping لمنع نوم الحاوية
# ---------------------------------------------------------------------------
async def ping_server() -> None:
    from aiohttp import web

    async def ping(request):
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/ping", ping)
    app.router.add_get("/healthz", ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info("خادم /ping يعمل على المنفذ %s", PORT)


# ---------------------------------------------------------------------------
# نقطة الدخول
# ---------------------------------------------------------------------------
async def main() -> None:
    global bot
    if not BOT_TOKEN:
        logger.error("لا يوجد BOT_TOKEN. اضبط متغير البيئة.")
        sys.exit(1)

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    # خادم /ping بالتوازي مع البوت
    asyncio.create_task(ping_server())

    # بدء الاستطلاع (polling) — يبقي البوت حياً دائماً
    logger.info("بدء تشغيل البوت بوضع polling...")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("تم إيقاف البوت.")
