# Playwright's image ships Chromium + all its system libraries (arm64 and amd64).
FROM mcr.microsoft.com/playwright/python:v1.60.0-noble

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    DISPLAY=:99

# Virtual display + VNC + noVNC so the bot's browser can be watched (and used to
# sign in) from the web UI.
RUN apt-get update \
 && apt-get install -y --no-install-recommends xvfb x11vnc fluxbox novnc websockify tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# --ignore-installed: apt pulled in Debian-managed python packages pip cannot uninstall
RUN pip install --no-cache-dir --ignore-installed -r requirements.txt
COPY linkedin_bot.py config.yaml targets.txt ./
COPY ui ./ui
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000 6080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/status', timeout=4)"
ENTRYPOINT ["/entrypoint.sh"]
