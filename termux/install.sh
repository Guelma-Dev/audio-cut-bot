#!/data/data/com.termux/files/usr/bin/bash
# تثبيت كل ما يحتاجه البوت على هاتف أندرويد (Termux)
# التشغيل مرة واحدة فقط

echo "=== تحديث الحزم ==="
pkg update -y && pkg upgrade -y

echo "=== تثبيت الأدوات ==="
pkg install -y python ffmpeg nodejs-lts git

echo "=== تثبيت مكتبات البوت ==="
pip install --upgrade pip
pip install aiogram yt-dlp aiohttp

echo ""
echo "✅ اكتمل التثبيت!"
echo "الآن انسخ ملف البوت:"
echo "    cp ~/storage/downloads/audio-cut-bot/main.py ~/audiobot/main.py"
echo "ثم شغّل:"
echo "    cd ~/audiobot && bash run.sh"
