FROM python:3.12-alpine

RUN apk add --no-cache ffmpeg curl unzip ca-certificates \
    && curl -fsSL https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-musl.zip -o /tmp/deno.zip \
    && unzip -o /tmp/deno.zip -d /usr/local/bin deno \
    && chmod +x /usr/local/bin/deno \
    && rm -f /tmp/deno.zip

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
