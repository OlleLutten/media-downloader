FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade "yt-dlp[default,curl-cffi]" "svtplay-dl==4.197" flask gunicorn

WORKDIR /app
COPY app.py /app/app.py
COPY templates /app/templates
COPY static /app/static

RUN mkdir -p /downloads

EXPOSE 8080
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "4", "--timeout", "0", "app:app"]
