"""End-to-end smoke test of the inbound WeCom callback path.

Starts the gateway on a scratch port with a **throwaway** token and
EncodingAESKey, then drives it exactly the way WeCom would:

  1. `GET  /wecom/callback?msg_signature=…&timestamp=…&nonce=…&echostr=…`
     — the URL verification WeCom performs when you save the callback config.
  2. `POST /wecom/callback?…` with a real AES-256-CBC encrypted, signed XML
     text message.
  3. The same POST with a tampered signature — must be rejected.

Nothing is sent to WeCom and nothing is written outside a temp directory. The
ERP is not required (handoff will fail without it; that is reported, not
treated as a smoke failure).

Why this exists: the signature scheme is `sha1(sort([token, timestamp, nonce,
msg_encrypt]))` — four values. A three-value implementation is self-consistent,
so it passes every unit test built the same wrong way, and rejects 100% of real
callbacks. Only a request built to the published spec catches that.

    python scripts/callback_smoke.py

Exit 0 = all checks passed.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import xml.etree.ElementTree as ET

GATEWAY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = sys.executable
PORT = int(os.environ.get("SMOKE_PORT", "8199"))
BASE = f"http://127.0.0.1:{PORT}"

OK = "  [ok]     "
NO = "  [FAIL]   "
INFO = "  [info]   "

failures = 0


def check(label: str, passed: bool, detail: str = "") -> bool:
    global failures
    print(f"{OK if passed else NO}{label}" + (f"  — {detail}" if detail else ""))
    if not passed:
        failures += 1
    return passed


# ---------------------------------------------------------------------------
# The WeCom crypto scheme, built from the published spec (doc path 90968)
# ---------------------------------------------------------------------------


def new_encoding_aes_key() -> str:
    """43 chars, exactly as the WeCom console generates it."""
    return base64.b64encode(secrets.token_bytes(32)).decode().rstrip("=")


def pkcs7_pad(data: bytes) -> bytes:
    pad = 32 - (len(data) % 32)
    return data + bytes([pad]) * pad


def encrypt_msg(plaintext_xml: str, aes_key: bytes, receiveid: str) -> str:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    body = plaintext_xml.encode("utf-8")
    rand_msg = (
        secrets.token_bytes(16) + struct.pack("!I", len(body)) + body + receiveid.encode("utf-8")
    )
    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(aes_key[:16]))
    encryptor = cipher.encryptor()
    return base64.b64encode(encryptor.update(pkcs7_pad(rand_msg)) + encryptor.finalize()).decode()


def sign(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    """sha1 of the four sorted values — token, timestamp, nonce, msg_encrypt."""
    return hashlib.sha1("".join(sorted([token, timestamp, nonce, encrypt])).encode()).hexdigest()


def envelope(encrypt: str, to_user: str, agent_id: str) -> str:
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{to_user}]]></ToUserName>"
        f"<Encrypt><![CDATA[{encrypt}]]></Encrypt>"
        f"<AgentID><![CDATA[{agent_id}]]></AgentID>"
        "</xml>"
    )


def inner_xml(msg_id: str, from_user: str, to_user: str, agent_id: str, content: str) -> str:
    created = int(time.time())
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{to_user}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
        f"<CreateTime>{created}</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{content}]]></Content>"
        f"<MsgId>{msg_id}</MsgId>"
        f"<AgentID><![CDATA[{agent_id}]]></AgentID>"
        "</xml>"
    )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def wait_for_health(proc: subprocess.Popen, timeout: float = 30.0) -> bool:
    import httpx

    deadline = time.time() + timeout
    with httpx.Client(trust_env=False, timeout=5) as client:
        while time.time() < deadline:
            if proc.poll() is not None:
                return False
            try:
                r = client.get(f"{BASE}/wecom/health")
                if r.status_code == 200:
                    return True
            except Exception:  # noqa: BLE001 - not up yet
                pass
            time.sleep(0.4)
    return False


def main() -> int:
    token = secrets.token_hex(8)
    encoding_aes_key = new_encoding_aes_key()
    aes_key = base64.b64decode(encoding_aes_key + "=")
    corp_id = "wwcc1546d13fbc5420"
    agent_id = "1000002"
    msg_id = f"smoke-{int(time.time())}"
    from_user = "wmSmokeTestUser001"
    plaintext = "明天需要 50 斤土豆和 20 斤西红柿"

    tmp = tempfile.mkdtemp(prefix="wecom-smoke-")
    env = dict(os.environ)
    env.update(
        WECOM_MODE="live",  # mock mode skips signature + decryption entirely
        WECOM_TOKEN=token,
        WECOM_ENCODING_AES_KEY=encoding_aes_key,
        WECOM_CORP_ID=corp_id,
        WECOM_AGENT_ID=agent_id,
        WECOM_PORT=str(PORT),
        WECOM_DATABASE_URL=f"sqlite:///{tmp}/smoke.db",
        WECOM_MEDIA_DIR=f"{tmp}/media",
        WECOM_MOCK_ARCHIVE_DIR=f"{tmp}/archive",
        WECOM_MOCK_MEDIA_DIR=f"{tmp}/mock-media",
        WECOM_OUTBOX_DIR=f"{tmp}/outbox",
        WECOM_SEND_ALLOWLIST="",  # nothing is sent anyway; keep the path clear
    )

    print("WeCom inbound callback smoke test")
    print("=" * 64)
    print(f"{INFO}scratch gateway on :{PORT}, temp dir {tmp}")
    print(f"{INFO}throwaway token={token}  aes_key={encoding_aes_key}")

    proc = subprocess.Popen(
        [PYTHON, "-m", "uvicorn", "app.main:app", "--port", str(PORT)],
        cwd=GATEWAY_DIR,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        import httpx

        if not check("gateway started", wait_for_health(proc)):
            err = (proc.stderr.read() if proc.stderr else b"").decode()[-2000:]
            print(err)
            return 1

        with httpx.Client(trust_env=False, timeout=15) as client:
            # --- 1. URL verification (GET) ---------------------------------
            print("\n1. GET /wecom/callback — URL verification")
            print("-" * 64)
            timestamp, nonce = str(int(time.time())), secrets.token_hex(4)
            echo_plain = f"echo-{secrets.token_hex(4)}"
            echostr = encrypt_msg(echo_plain, aes_key, corp_id)
            query = urllib.parse.urlencode(
                {
                    "msg_signature": sign(token, timestamp, nonce, echostr),
                    "timestamp": timestamp,
                    "nonce": nonce,
                    "echostr": echostr,
                }
            )
            r = client.get(f"{BASE}/wecom/callback?{query}")
            check(
                "returns 200 with the decrypted echo",
                r.status_code == 200 and r.text == echo_plain,
                f"http={r.status_code} body={r.text[:60]!r}",
            )

            # --- 2. Real encrypted message (POST) --------------------------
            print("\n2. POST /wecom/callback — signed, encrypted text message")
            print("-" * 64)
            timestamp, nonce = str(int(time.time())), secrets.token_hex(4)
            encrypt = encrypt_msg(
                inner_xml(msg_id, from_user, corp_id, agent_id, plaintext), aes_key, corp_id
            )
            body = envelope(encrypt, corp_id, agent_id)
            query = urllib.parse.urlencode(
                {
                    "msg_signature": sign(token, timestamp, nonce, encrypt),
                    "timestamp": timestamp,
                    "nonce": nonce,
                }
            )
            r = client.post(
                f"{BASE}/wecom/callback?{query}",
                content=body.encode("utf-8"),
                headers={"Content-Type": "application/xml"},
            )
            check("accepted (not 403)", r.status_code == 200, f"http={r.status_code}")
            if r.status_code == 200:
                data = r.json()
                print(f"{INFO}response: {json.dumps(data, ensure_ascii=False)[:160]}")
                check("ingestor ran", bool(data.get("ok")), f"ok={data.get('ok')}")

            # --- 3. The message actually landed ----------------------------
            print("\n3. Message recorded")
            print("-" * 64)
            r = client.get(f"{BASE}/wecom/messages", params={"page_size": 500})
            rows = r.json().get("items", []) if r.status_code == 200 else []
            row = next((m for m in rows if m.get("msgid") == msg_id), None)
            check("row exists for our msgid", row is not None, f"msgid={msg_id}")
            if row:
                print(f"{INFO}msgid={row.get('msgid')} status={row.get('status')}")
                print(f"{INFO}from={row.get('external_userid')} source_type={row.get('source_type')}")
                check("classified as text", row.get("source_type") == "text")
                check("sender captured", row.get("external_userid") == from_user)
                if row.get("status") == "failed":
                    print(f"{INFO}handoff failed — the ERP is not running. Expected here.")

            # --- 4. Tampered signature is rejected -------------------------
            print("\n4. Tampered signature is rejected")
            print("-" * 64)
            timestamp, nonce = str(int(time.time())), secrets.token_hex(4)
            encrypt = encrypt_msg(
                inner_xml(msg_id + "-bad", from_user, corp_id, agent_id, plaintext),
                aes_key,
                corp_id,
            )
            query = urllib.parse.urlencode(
                {
                    "msg_signature": "0" * 40,
                    "timestamp": timestamp,
                    "nonce": nonce,
                }
            )
            r = client.post(
                f"{BASE}/wecom/callback?{query}",
                content=envelope(encrypt, corp_id, agent_id).encode("utf-8"),
                headers={"Content-Type": "application/xml"},
            )
            check("rejected with 403", r.status_code == 403, f"http={r.status_code}")

            # --- 5. Signature over the wrong payload is rejected -----------
            print("\n5. Signature for a different payload is rejected")
            print("-" * 64)
            timestamp, nonce = str(int(time.time())), secrets.token_hex(4)
            encrypt_a = encrypt_msg(
                inner_xml(msg_id + "-a", from_user, corp_id, agent_id, "A"), aes_key, corp_id
            )
            encrypt_b = encrypt_msg(
                inner_xml(msg_id + "-b", from_user, corp_id, agent_id, "B"), aes_key, corp_id
            )
            # Sign A, send B: proves the payload is part of the signature.
            query = urllib.parse.urlencode(
                {
                    "msg_signature": sign(token, timestamp, nonce, encrypt_a),
                    "timestamp": timestamp,
                    "nonce": nonce,
                }
            )
            r = client.post(
                f"{BASE}/wecom/callback?{query}",
                content=envelope(encrypt_b, corp_id, agent_id).encode("utf-8"),
                headers={"Content-Type": "application/xml"},
            )
            check("rejected with 403", r.status_code == 403, f"http={r.status_code}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # noqa: F821
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 64)
    if failures:
        print(f"FAILED — {failures} check(s)")
    else:
        print("PASSED — inbound callback path works against the real scheme")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
