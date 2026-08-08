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

echo "=== تنزيل ملف البوت تلقائياً ==="
mkdir -p ~/audiobot
curl -sL https://raw.githubusercontent.com/Guelma-Dev/audio-cut-bot/main/main.py -o ~/audiobot/main.py
curl -sL https://raw.githubusercontent.com/Guelma-Dev/audio-cut-bot/main/termux/run.sh -o ~/audiobot/run.sh
chmod +x ~/audiobot/run.sh

echo ""
echo "✅ اكتمل التثبيت!"
echo "لتشغيل البوت اكتب:"
echo "    cd ~/audiobot && bash run.sh"
