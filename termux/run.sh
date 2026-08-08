#!/data/data/com.termux/files/usr/bin/bash
# تشغيل البوت على الهاتف
# لإيقافه: اضغط Ctrl+C في هذه النافذة

cd ~/audiobot

# معلومات البوت - عدّلها هنا إذا لزم
export BOT_TOKEN="8674306931:AAHyD_oxxW8xRoEWn1UhWXDYz1ploQlIn5M"
export ALLOWED_USER_IDS="6586489447"
export PORT="9090"

echo "=== تشغيل البوت على هاتفك ==="
echo "لإيقاف البوت: اضغط Ctrl + C"
echo ""
python main.py
