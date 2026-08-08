FROM python:3.10-slim

# FFmpeg + Node.js (بيئة JavaScript لحل تحديات توقيع yt-dlp nsig)
# + curl لحقن الصحة
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    nodejs \
    npm \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

ENV PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["python", "main.py"]
