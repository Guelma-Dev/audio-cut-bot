# النشر على Railway

## الخطوة 1: إنشاء حساب
1. افتح https://railway.app وابدأ بإنشاء حساب عبر **GitHub**.
2. بعد الدخول، اضغط **New Project** ثم **Deploy from GitHub repo**.
3. اختر المستودع `Guelma-Dev/audio-cut-bot` (إذا لم يظهر، اضغط Configure GitHub App وامنح الوصول للمستودع).

## الخطوة 2: إعداد المتغيرات البيئية
بعد إنشاء الخدمة، افتح تبويب **Variables** (المتغيرات) وأضف:

| المتغير | القيمة |
|---------|--------|
| `BOT_TOKEN` | `8882251698:AAFARPlCXp6zy0nUByX8MTAEbuDmPcNXeSM` |
| `ALLOWED_USER_IDS` | `6586489447` |
| `COOKIES_TXT_B64` | ضع المحتوى الكامل من ملف `/tmp/fresh_cookies_b64.txt` على جهازك |
| `BGUTIL_SERVER_HOME` | `/opt/bgutil-ytdlp-pot-provider/server` |
| `DENO_PATH` | `/usr/bin/deno` |
| `WEBHOOK_URL` | يحدد بعد معرفة رابط الخدمة (انظر الخطوة 3) |
| `WEBHOOK_SECRET` | `awSi7OkHkj98QPpjV5jvNV1cPDLC9SQL` |

ملاحظة: `PORT` يوفره Railway تلقائياً، والكود يقرأه.

## الخطوة 3: إصلاح رابط الـ webhook
1. افتح تبويب **Settings** → **Networking** وانسخ الـ **Public Networking Domain**.
   مثال: `https://audio-cut-bot-production.up.railway.app`
2. عدّل متغير `WEBHOOK_URL` ليكون `https://<اسم-خدمتك>.up.railway.app`
3. الكود يرسل webhook تلقائياً عند الإقلاع، لذا أعد النشر (Deploy) بعد ضبطه.

## الخطوة 4: تعطيل Render (أو إيقافه)
بعد التأكد أن Railway يعمل:
- عطّل الـ webhook القديم من Render حتى لا يتنازع الطرفان.
- يمكنك إيقاف خدمة Render من لوحة التحكم (على الأقل مؤقتاً لحين النشر الاحتياطي).

## ملاحظات
- `COOKIES_TXT_B64` ستنتهي صلاحيتها عندما يٌدار الكوكيز في المتصفح — أعد تصديرها عند الحاجة (يوجد سكربت التصدير في /tmp).
- خطة Railway المجانية تعطي 500 ساعة/شهر — تكفي لتشغيل مستمر 20.8 يوماً؛ عند نفادها سيتم إيقاف الخدمة حتى بداية الشهر التالي.
