#!/usr/bin/env bash
#
# Fetch the official WeCom finance SDK (会话内容存档) — the library the ARCHIVE
# MEDIA path needs.
#
#   bash scripts/fetch_sdk.sh              # -> ./vendor/libWeWorkFinanceSdk_C.so
#   bash scripts/fetch_sdk.sh /opt/sdk     # -> /opt/sdk/libWeWorkFinanceSdk_C.so
#
# Why you need it: text messages come back from the archive API and are
# decryptable in pure Python, so text needs no SDK. ATTACHMENTS do not work that
# way — `image`, `file`, `voice` and `mixed` can ONLY be fetched through this
# vendor library. Without it the first attachment a customer sends fails, and
# because `pull_once` holds the archive cursor on a failed entry, it then BLOCKS
# every message behind it. A customer order is usually an attachment.
#
# Two notes before you run it:
#   1. The library is Linux x86-64. It cannot be loaded on macOS, so a local
#      checkout can download it but not use it. The deploy host is what matters,
#      and Railway fetches it automatically at boot.
#   2. Both digests are pinned and checked. The `.so` digest matches the vendor's
#      own `md5.txt`, which ships inside the archive.
#
# Every command here is a single line on purpose — no line continuations.
set -euo pipefail

URL="https://wwcdn.weixin.qq.com/node/wwcomm/sdk_x86_v3_20250205.tgz"
TARBALL_MD5="838e1613abeb874d697f58913b61b945"
SO_MD5="f2db3dd1372c516db6290afbd1b5c698"

DEST_DIR="${1:-$(cd "$(dirname "$0")/.." && pwd)/vendor}"
DEST="$DEST_DIR/libWeWorkFinanceSdk_C.so"

mkdir -p "$DEST_DIR"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

md5_of() {
  if command -v md5sum >/dev/null 2>&1; then
    md5sum "$1" | awk '{print $1}'
  else
    md5 -q "$1"
  fi
}

echo "Downloading $URL"
curl -fsSL --noproxy '*' -o "$TMP/sdk.tgz" "$URL"

GOT="$(md5_of "$TMP/sdk.tgz")"
if [ "$GOT" != "$TARBALL_MD5" ]; then
  echo "FAIL: tarball md5 is $GOT, expected $TARBALL_MD5" >&2
  exit 1
fi
echo "  tarball md5 ok"

tar -xzf "$TMP/sdk.tgz" -C "$TMP" --strip-components=1 "C_sdk/libWeWorkFinanceSdk_C.so"

GOT="$(md5_of "$TMP/libWeWorkFinanceSdk_C.so")"
if [ "$GOT" != "$SO_MD5" ]; then
  echo "FAIL: library md5 is $GOT, expected $SO_MD5" >&2
  exit 1
fi
echo "  library md5 ok"

mv "$TMP/libWeWorkFinanceSdk_C.so" "$DEST"
chmod 755 "$DEST"

echo
echo "OK -> $DEST"
echo
echo "Set these on the service, then redeploy:"
echo "  WECOM_DECRYPT_PROVIDER=sdk"
echo "  WECOM_ARCHIVE_SDK_PATH=$DEST"
echo
echo "Leave WECOM_ARCHIVE_SDK_PATH empty instead if you want the gateway to"
echo "fetch the library itself at boot (WECOM_ARCHIVE_SDK_AUTOFETCH=true)."
