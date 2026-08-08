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
| `WEBHOOK_URL` | يحدد بعد معرفة رابط الخدمة (انظر الخطوة 3) |
| `WEBHOOK_SECRET` | `awSi7OkHkj98QPpjV5jvNV1cPDLC9SQL` |

ملاحظة: `PORT` يوفره Railway تلقائياً، والكود يقرأه. النسخة الجديدة لا تحتاج `BGUTIL_SERVER_HOME` ولا `DENO_PATH`.

## الخطوة 3: إصلاح رابط الـ webhook
1. افتح تبويب **Settings** → **Networking** وانسخ الـ **Public Networking Domain**.
   مثال: `https://audio-cut-bot-production.up.railway.app`
2. عدّل متغير `WEBHOOK_URL` ليكون `https://<اسم-خدمتك>.up.railway.app`
3. الكود يرسل webhook تلقائياً عند الإقلاع، لذا أعد النشر (Deploy) بعد ضبطه.

## الخطوة 4: اختبار قبل تعطيل Render
- أرسل فيديو يوتيوب للبوت من Railway.
- إذا عمل، عطّل الـ webhook القديم من Render (من لوحة Render أو حذف `WEBHOOK_URL`).

## الخطوة 5: التحقق من IP الصادر
بعد النشر، افتح: `https://<اسم-خدمتك>.up.railway.app/diag`
- انظر `outbound_ip` — إذا كان IP مختلفاً عن `74.220.51.139` (AWS محظور من YouTube) فهناك فرصة للنجاح.
- إذا كان `test_android_vr_no_cookies` يساوي `OK` فالبوت يعمل.

## ملاحظات
- `COOKIES_TXT_B64` ستنتهي صلاحيتها عندما يُدار الكوكيز في المتصفح — أعد تصديرها عند الحاجة.
- خطة Railway المجانية تعطي 500 ساعة/شهر — تكفي لتشغيل مستمر 20.8 يوماً.
- إذا حُظر IP Railway أيضاً، ستحتاج الوكيل المنزلي (Tailscale) أو proxy مدفوع — أخبر المطوّر.
