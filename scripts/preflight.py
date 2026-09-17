"""WeCom live-readiness preflight.

Read-only. Nothing is sent to any customer, nothing is written to WeCom.

It answers three questions, in order:

  1. Are the credentials in `.env` valid?          -> `gettoken`
  2. Is this machine allowed to call the business
     APIs?                                        -> `agent/get`
  3. What is still missing before `WECOM_MODE=live`?

`gettoken` succeeds from any IP, so step 1 passing does **not** mean you are
ready to go live. Step 2 is the real gate: WeCom requires the caller's public
IP to be in the corp's 可信IP allow-list, and returns `errcode 60020`
("not allow to access from your ip") otherwise. That is a console setting —
no code change can work around it.

Run from the gateway root (or anywhere; the script chdirs so `.env` resolves):

    python scripts/preflight.py

Exit code 0 = live-ready, 1 = something is still blocking.
"""
from __future__ import annotations

import os
import re
import sys

GATEWAY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, GATEWAY_DIR)
# `.env` is resolved relative to the working directory, so land there before
# settings is imported. Running from the repo root would silently load the
# ERP's `.env` instead and report every credential as missing.
_previous_cwd = os.getcwd()
os.chdir(GATEWAY_DIR)

import httpx  # noqa: E402

# Import the pull path rather than spelling it out here. This script previously
# hard-coded `/msgaudit/get_chat_data`, which is the ONE spelling that 404s with
# an empty body — so the preflight the README tells you to run before going live
# reported the archive as broken even after it worked. Importing the constant
# makes that drift impossible.
from app.adapters.wecom_api import ARCHIVE_PULL_PATH  # noqa: E402
from app.core.config import settings  # noqa: E402

# Put the caller back where they were — this module is also imported by tests,
# and a stray chdir would silently repoint every relative path in the suite.
os.chdir(_previous_cwd)

BASE = "https://qyapi.weixin.qq.com/cgi-bin"
IP_RE = re.compile(r"from ip:\s*([0-9a-fA-F:.]+)")

OK = "  [ok]     "
NO = "  [blocked]"
WARN = "  [todo]   "
INFO = "  [info]   "


def mask(value: str, keep: int = 4) -> str:
    if not value:
        return "(empty)"
    return value[:keep] + "*" * max(0, len(value) - keep)


def main() -> int:
    blocking = 0

    print("WeCom preflight")
    print("=" * 60)

    # --- 1. configuration ------------------------------------------------
    print("\nConfiguration  (wecom-gateway/.env)")
    print("-" * 60)
    print(f"{INFO}mode                 = {settings.mode}"
          f"{'' if settings.is_live else '   (mock: nothing is sent to WeCom)'}")
    print(f"{INFO}corp_id              = {settings.corp_id or '(empty)'}")
    print(f"{INFO}agent_id             = {settings.agent_id or '(empty)'}")
    print(f"{INFO}secret               = {mask(settings.secret)}")
    allowlist = settings.send_allowlist_set()
    if allowlist:
        print(f"{OK}send_allowlist       = {', '.join(sorted(allowlist))}")
        print(f"{INFO}  live sends to anything else are refused, not delivered")
    else:
        print(f"{WARN}send_allowlist       = (empty)   live sends go to any resolved destination")

    for label, value, why in (
        ("token", settings.token, "inbound callbacks (app callback URL)"),
        ("encoding_aes_key", settings.encoding_aes_key, "inbound callbacks (app callback URL)"),
        ("archive_private_key_path", settings.archive_private_key_path, "session archive decryption"),
    ):
        if value:
            shown = value if label.endswith("path") else mask(value)
            print(f"{OK}{label:<24} = {shown}")
            if label.endswith("path") and not os.path.exists(value):
                print(f"{NO}    file does not exist: {value}")
                blocking += 1
        else:
            print(f"{WARN}{label:<24} = (empty)   needed for {why}")

    if not (settings.corp_id and settings.agent_id and settings.secret):
        print(f"\n{NO}corp_id / agent_id / secret are required. Nothing else can be checked.")
        return 1

    # --- 2. credentials --------------------------------------------------
    print("\nCredentials  (GET /cgi-bin/gettoken)")
    print("-" * 60)
    client = httpx.Client(trust_env=False, timeout=20)
    try:
        resp = client.get(
            f"{BASE}/gettoken",
            params={"corpid": settings.corp_id, "corpsecret": settings.secret},
        )
        data = resp.json()
    except Exception as exc:  # network/DNS/TLS
        print(f"{NO}could not reach WeCom: {exc}")
        return 1

    errcode = data.get("errcode")
    if errcode != 0:
        print(f"{NO}errcode={errcode} errmsg={data.get('errmsg')}")
        print(f"{INFO}corp_id and/or secret are wrong, or the app is disabled.")
        return 1

    print(f"{OK}errcode=0  token issued (expires in {data.get('expires_in')}s)")
    token = data["access_token"]

    # --- 3. business API / trusted IP -----------------------------------
    print("\nTrusted IP  (GET /cgi-bin/agent/get)")
    print("-" * 60)
    resp = client.get(
        f"{BASE}/agent/get",
        params={"access_token": token, "agentid": settings.agent_id},
    )
    data = resp.json()
    errcode = data.get("errcode")
    if errcode == 0:
        name = data.get("name", "(unnamed)")
        print(f"{OK}errcode=0  agent reachable: {name!r}")
        print(f"{INFO}sending is possible from this machine.")
    else:
        errmsg = data.get("errmsg", "")
        ip_match = IP_RE.search(errmsg)
        print(f"{NO}errcode={errcode} errmsg={errmsg}")
        if errcode == 60020 and ip_match:
            print(f"\n{INFO}Your public IP is {ip_match.group(1)}")
            print(f"{INFO}Add it to the corp allow-list:")
            print(f"{INFO}  WeCom admin -> 我的企业 -> 企业信息 -> 可信IP")
            print(f"{INFO}(or the app's own 企业可信IP setting, if it overrides the corp list)")
        elif errcode == 60011:
            print(f"{INFO}No privilege for this agentid — check WECOM_AGENT_ID.")
        print(f"\n{INFO}Until this passes, every live send fails. Stay in mock mode.")
        blocking += 1

    # --- 4. session archive ----------------------------------------------
    # The archive is the ONLY inbound path for real customer orders (the smart
    # bot is internal-groups-only), so this check matters more than the send
    # checks above. It is read-only: `get_chat_data` pulls, it never sends.
    print(f"\nSession Archive  (POST /cgi-bin{ARCHIVE_PULL_PATH})")
    print("-" * 60)
    archive_secret = (settings.archive_secret or "").strip()
    if not archive_secret:
        print(f"{NO}WECOM_ARCHIVE_SECRET is not set — the archive cannot be pulled.")
        print(f"{INFO}It is NOT WECOM_SECRET. Get it from:")
        print(f"{INFO}  Admin Console -> Security & Management -> Management Tools")
        print(f"{INFO}  -> Conversation Content Archiving")
        blocking += 1
    else:
        print(f"{INFO}archive_secret       = {mask(archive_secret)}")
        try:
            resp = client.get(
                f"{BASE}/gettoken",
                params={"corpid": settings.corp_id, "corpsecret": archive_secret},
            )
            adata = resp.json()
        except Exception as exc:  # noqa: BLE001
            print(f"{NO}archive token request failed: {exc}")
            adata = {}

        if adata.get("errcode"):
            print(f"{NO}archive gettoken errcode={adata.get('errcode')} "
                  f"errmsg={adata.get('errmsg')}")
            print(f"{INFO}The archive secret is wrong, or the feature is not activated.")
            blocking += 1
        else:
            print(f"{OK}archive token issued")
            atoken = adata["access_token"]
            # limit=1 keeps the probe cheap. This is a READ; nothing is sent.
            resp = client.post(
                f"{BASE}{ARCHIVE_PULL_PATH}",
                params={"access_token": atoken},
                json={"seq": 0, "limit": 1, "timeout": settings.archive_timeout},
            )
            # A wrong path answers 404 with an EMPTY body, and `.json()` on that
            # raises JSONDecodeError — which used to escape as a traceback and
            # tell you nothing. Decode defensively and name the real cause.
            try:
                gdata = resp.json()
            except Exception:  # noqa: BLE001
                gdata = None

            if gdata is None:
                print(f"{NO}archive pull returned a non-JSON response: "
                      f"HTTP {resp.status_code}, "
                      f"content-type={resp.headers.get('content-type')!r}, "
                      f"body={resp.text[:120]!r}")
                print(f"{INFO}A 404 with an empty body means the PATH is wrong, not the "
                      f"token. The pull lives at /cgi-bin{ARCHIVE_PULL_PATH}.")
                print(f"{INFO}Do NOT move it under /msgaudit/ — that namespace answers 404 "
                      f"with an empty body for THIS call, even though sibling /msgaudit/ "
                      f"paths return proper JSON. See ARCHIVE_PULL_PATH's comment.")
                blocking += 1
            else:
                gerr = gdata.get("errcode")
                if gerr == 0:
                    n = len(gdata.get("chatdata", []) or [])
                    print(f"{OK}archive pull reachable (returned {n} "
                          f"entr{'y' if n == 1 else 'ies'})")
                    print(f"{INFO}The archive is pullable from this IP. Inbound is live-capable.")
                    if n == 0:
                        print(f"{INFO}0 entries is a normal result, not a failure — it can "
                              f"mean no in-scope member has sent anything yet. Confirm WHO "
                              f"is in scope with GET /wecom/archive/scope before assuming "
                              f"the pull is broken.")
                else:
                    errmsg = gdata.get("errmsg", "")
                    ip_match = IP_RE.search(errmsg)
                    print(f"{NO}archive pull errcode={gerr} errmsg={errmsg}")
                    if gerr == 60020 and ip_match:
                        print(f"\n{INFO}This IP is not allow-listed: {ip_match.group(1)}")
                        print(f"{INFO}Add it under Admin Console -> My Enterprise -> 可信IP.")
                        print(f"{INFO}On Railway the egress IP is DYNAMIC — it will change on")
                        print(f"{INFO}redeploy, so a fixed-IP host or proxy is the real fix.")
                    elif gerr == 60011:
                        print(f"{INFO}No privilege — the archive is not enabled for this corp.")
                    elif gerr == 10009:
                        print(f"{INFO}10009 = the calling IP is not in the archive's Trusted")
                        print(f"{INFO}IP list. That list is SEPARATE from 可信IP: set it on")
                        print(f"{INFO}the Message Archiving page, before you enable it.")
                    blocking += 1

    # --- 5. verdict ------------------------------------------------------
    print("\n" + "=" * 60)
    missing = [
        name
        for name, value in (
            ("WECOM_TOKEN", settings.token),
            ("WECOM_ENCODING_AES_KEY", settings.encoding_aes_key),
            ("WECOM_ARCHIVE_SECRET", settings.archive_secret),
            ("WECOM_ARCHIVE_PRIVATE_KEY_PATH", settings.archive_private_key_path),
        )
        if not value
    ]
    if blocking:
        print("NOT READY for live mode.")
        if settings.is_live:
            print(f"{NO}WECOM_MODE=live but the business API is unreachable — every send fails.")
            print(f"{INFO}Set WECOM_MODE=mock until the allow-list is updated.")
    elif missing:
        print("PARTIALLY READY — WeCom is reachable, but these are still unset:")
        for name in missing:
            print(f"{WARN}{name}")
        print(f"{INFO}You can go live for outbound; inbound/archive stay off.")
    else:
        print("READY for live mode.")
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())
