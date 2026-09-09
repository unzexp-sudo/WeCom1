# WeCom Gateway — custom image for Railway.
#
# Why a custom image instead of NIXPACKS: the Session Archive (会话内容存档) media
# download needs WeCom's official C SDK (libWeWorkFinanceSdk.so), which NIXPACKS
# cannot inject. This image:
#   1. is Debian bookworm-based (glibc + OpenSSL 3.0) — required by the v3.0 SDK
#      (DO NOT use Alpine/musl; the .so is built against glibc).
#   2. fetches the x86_64 v3.0 SDK .so at build time from WeCom's CDN.
#   3. expects the RSA PRIVATE KEY PEM to be mounted at runtime (Railway Volume)
#      at $WECOM_ARCHIVE_PRIVATE_KEY_PATH — it is a SECRET, never baked in.
#
# Build args / env used:
#   WECOM_ARCHIVE_SDK_PATH       default /app/libWeWorkFinanceSdk.so
#   WECOM_ARCHIVE_PRIVATE_KEY_PATH  mounted secret, e.g. /app/secrets/archive_private_key.pem
#   WECOM_DECRYPT_PROVIDER=sdk
#   WECOM_ARCHIVE_SECRET         the 会话内容存档 secret (NOT the app secret)
#   PORT                         injected by Railway
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps: libssl3 is already in bookworm; ensure it for the SDK's OpenSSL 3.0
# linkage. curl is only needed at build to fetch the .so.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates libssl3 \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first for better layer caching.
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy application source.
COPY app ./app
COPY scripts ./scripts
# If you have a simulator/ tests you want in the image, add them here.

# --- WeCom finance SDK (.so) ---------------------------------------------------
# x86_64 v3.0 (OpenSSL 3.0, 2025-02-13). Railway runs x86_64, so do NOT use the
# arm build. If the CDN blocks hotlinking, download the .tgz locally, extract
# libWeWorkFinanceSdk.so next to this Dockerfile, and replace the RUN below with:
#   COPY libWeWorkFinanceSdk.so /app/libWeWorkFinanceSdk.so
RUN curl -fL -A "Mozilla/5.0" \
    "https://wwcdn.weixin.qq.com/node/wwcomm/sdk_x86_v3_20250205.tgz" \
    -o /tmp/sdk.tgz \
    && tar -xzf /tmp/sdk.tgz -C /tmp \
    && find /tmp -name 'libWeWorkFinanceSdk.so' -exec cp {} /app/libWeWorkFinanceSdk.so \; \
    && rm -f /tmp/sdk.tgz \
    && test -f /app/libWeWorkFinanceSdk.so && echo "SDK .so present"

# Directory for the mounted private-key secret (Railway Volume). The file itself
# must NOT be committed to the repo.
RUN mkdir -p /app/secrets

# Runtime mount point for persistent media/DB if you attach a Railway Volume.
# (Optional — only needed if you want downloaded media to survive restarts.)
RUN mkdir -p /app/data/wecom

EXPOSE 8100
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8100}"]
