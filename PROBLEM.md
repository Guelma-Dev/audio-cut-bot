# وصف كامل للمشكلة: بوت تيليجرام لقص الصوتيات من يوتيوب

## 1. فكرة المشروع
بوت تيليجرام (Python) يستقبل رابط فيديو يوتيوب، يعرض معلوماته، ويقصّ مقطعاً صوتياً
(MP3) من توقيت إلى آخر أو الصوت كاملاً، ثم يرسله للمستخدم.

### التقنيات
- Python 3.12 + python-telegram-bot (v21) + yt-dlp + FastAPI + uvicorn
- Docker على استضافة مجانية
- وضع العمل على السحابة: Webhook على Render (Free instance)
- المتغيرات البيئية على Render:
  - BOT_TOKEN
  - ALLOWED_USER_IDS
  - WEBHOOK_URL = https://audio-cut-bot.onrender.com
  - WEBHOOK_SECRET
  - COOKIES_TXT (نص كوكيز يوتيوب — انتهت صلاحيتها الآن)
  - COOKIES_TXT_B64 (كوكيز يوتيوب جديدة صالحة بصيغة Base64 — تُفك وتكتب كملف)

### إعدادات yt-dlp
- العملاء (player clients): android_vr، tv، web_safari، web_embedded
- تُمرَّر الكوكيز عبر cookiefile (ملف Netscape)

## 2. السيناريو الزمني (الأهم)

- **أمس (08-07 / بداية 08-08):** البوت كان يعمل بشكل ممتاز على Render.
  آخر نسخة عملت هي commit c47c789 (yt-dlp مع android_vr/tv/web_safari/web_embedded
  + cookiefile + ejs:github + js_runtimes node/deno).
- **ثم طلب المستخدم تحسينات** (قصّ m4a بنسخ مباشر -c:a copy، إزالة وسائط غير مدعومة،
  تسريع جلب المعلومات بعميل android_vr فقط). هذه التعديلات اشتغلت **محلياً** لكن على
  Render بدأت تظهر رسالة خطأ للمستخدم.
- **بعدها** بدأنا إصلاحات كثيرة: تثبيت yt-dlp-ejs، bgutil-ytdlp-pot-provider (سيرفر
  PO Token)، deno، تحديث الكوكيز، إلخ. كلها لم تحل.
- **اليوم:** عدنا إلى نسخة أمس بالضبط (c47c789) وأعدنا بناء نظيفاً من الصفر،
  وما زالت نفس المشكلة على Render.

## 3. المشكلة بالضبط

عند إرسال أي رابط يوتيوب على Render تظهر الرسالة:
**"Sign in to confirm you're not a bot. Use --cookies-from-browser or --cookies for the authentication."**

ويترجمها البوت للمستخدم إلى: "هذا الفيديو يتطلب تسجيل الدخول لتأكيد العمر ولا يمكن تنزيله."

### ما تحقق بالاختبار القاطع (نفس الفيديو rupFLbOkioQ، نفس إعدادات yt-dlp):

| المكان | IP | النتيجة |
|---|---|---|
| محلياً (بيت المستخدم) | IP منزلي | ✓ ينجح: 31 صيغة |
| Render (السحابة) | IP مركز بيانات | ✗ "Sign in to confirm you're not a bot" |

- الكوكيز الجديدة (COOKIES_TXT_B64) صالحة ومكتملة (PSID, HSID, SID, SAPISID, __Secure-3PSID موجودة).
- حتى بدون كوكيز إطلاقاً، محلياً يعمل android_vr بنجاح.
- على Render تفشل **كل** العملاء: android_vr, tv, mweb, web_safari, tv_nocookies،
  مع وبدون كوكيز، مع وبدون PO Token.
- تغيير user-agent أو headers لم يُحاول بالتفصيل بعد.

## 4. الاستنتاج الذي توصلنا إليه (قابل للنقاش)

YouTube يحظر IP مراكز البيانات (خصوصاً Render) بفحص "Sign in to confirm you're not a bot"
حتى مع جلسة دخول صالحة. محلياً نفس الكوكيز تعمل. هذا يرجّح أن المشكلة IP لا كود.

**لكن** المستخدم غير مقتنع (يرى أن أمس عمل واليوم توقف فجأة بدون تغيير IP معروف).

## 5. فرضيات بديلة لم تُفحص بالكامل

1. **تغيّر IP الصادر لـ Render** (Render يغيّر نطاق IP أحياناً) — قابل للفحص بمقارنة IP اليوم.
2. **الـ cookies من المتصفح**: أمس الكوكيز كانت من Firefox (تحتوي LOGIN_INFO و__Host-1PLSID)
   واليوم جديدة من Chrome (لا تحتوي LOGIN_INFO و__Host-1PLSID). هذه الفروق قد تؤثر.
3. **تحسينات yt-dlp أو إصداره** تغيّر سلوك إرسال الكوكيز.
4. **لا يوجد كود يعالج "Sign in" بإعادة المحاولة بعد كسر التخزين المؤقت أو الـ client_name مختلف**.
5. **Webhook vs Polling**: أمس ربما كان على Back4App/Polling واليوم على Render/Webhook —
   لكن الـ IP هو نفسه مصدر المشكلة، ليس الوضع.

## 6. ما نحتاجه من الحل
- جعل البوت يعمل على استضافة سحابية مجانية (Render حالياً) بدون "Sign in to confirm".
- حلول مقترحة للبحث عنها:
  - استخدام Proxy/Residential IP
  - استخدام مكتبات بديلة مثل yt-dlp مع --extractor-args youtube:player_client=android_vr
    و iOS وغيره بتجربة أكثر
  - استخدام الحلول الجاهزة: youtube_transcript_api، pytube، cobalt، إلخ
  - التحقق من IP الصادر الحالي لـ Render: curl ifconfig.me من داخل الحاوية

## 7. تفاصيل تقنية دقيقة

### الإصدارات المستخدمة
- yt-dlp: 2026.7.4
- python-telegram-bot: 22.8
- Python: 3.12-alpine (Docker)
- ffmpeg + nodejs مثبتان (لتوقيع nsig عبر EJS)

### أمر إعادة إنتاج المشكلة (من داخل Render container)
```bash
curl -s ifconfig.me  # لمعرفة IP الصادر الحالي
yt-dlp --cookies /tmp/cookies.txt --extractor-args "youtube:player_client=android_vr,tv" "https://www.youtube.com/watch?v=rupFLbOkioQ" -f "ba" -o "/dev/null"
# النتيجة على Render: "Sign in to confirm you're not a bot"
# نفس الأمر محلياً: ينجح
```

### سلوك العملاء على Render (كلها فشلت)
- android_vr (بدون كوكيز): Sign in
- tv (مع كوكيز صالحة): Sign in
- tv (بدون كوكيز): Sign in
- mweb (مع bgutil PO Token): Sign in
- web_safari (مع كوكيز): Sign in

### على IP منزلي
- android_vr بدون أي شيء: ينجح (31 format)
- إعدادات أمس بالضبط (android_vr,tv,web_safari,web_embedded + ejs): ينجح (31 format)

### الملاحظة الأخيرة الحاسمة
حتى نسخة أمس c47c789 المستعادة حرفياً تعمل محلياً وترفض على Render.
الاختلاف الوحيد بين البيئتين هو IP المصدر. إما IP Render محظور الآن،
أو هناك فرق بيئة آخر لم يُكتشف (متغيرات بيئة، DNS، أذونات، إلخ).
