FROM python:3.13.7-slim-bookworm

LABEL org.opencontainers.image.source="https://github.com/jesp1999/youtarr-routarr" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# ffmpeg and ffprobe extract and tag audio for the Music library.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY LICENSE THIRD_PARTY_NOTICES.md wsgi.py ./
COPY youtarr_routarr ./youtarr_routarr
# COPY keeps host modes, and a host umask of 007 leaves files unreadable to the app user.
RUN chmod -R a+rX /app

USER 1000:1000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"]

# One worker so the queue-ping thread and the filing lock each exist once.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "4", "--worker-tmp-dir", "/dev/shm", "wsgi:app"]
