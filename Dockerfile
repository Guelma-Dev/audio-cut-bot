FROM python:3.12-alpine

RUN apk add --no-cache ffmpeg nodejs ca-certificates

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

ENV PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["python", "main.py"]
