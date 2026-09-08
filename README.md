# WeCom Gateway (企业微信网关)

A small, standalone FastAPI service that sits between **WeCom (企业微信)** and the
ERP in `../backend/`.

It does exactly four things:

1. **Ingests** customer messages (text, image, PDF, Excel) from the WeCom Session
   Archive (会话存档) stream.
2. **Deduplicates** on `msgid` — WeCom retries, so the same message can arrive twice.
3. **Resolves identity** — works out which ERP `customer_id` sent the message, using
   a five-step cascade (see below).
4. **Hands off** to the ERP intake pipeline (`POST {ERP}/api/v1/intake/wecom`) and
   logs everything for the ops console.

It also carries traffic the other way: the ERP calls `POST /wecom/send` to push
templated notifications (order confirmed, out for delivery, invoice ready, …) back to
the customer or to an ops group.

**It never parses an order itself.** Parsing, matching and pricing stay in the ERP.

The authoritative contract is [`../docs/WECOM_CONTRACTS.md`](../docs/WECOM_CONTRACTS.md).
Read that before changing behaviour.

---

## Status: credentials validated, no message ever sent

Credentials were supplied on 2026-09-08 and checked against the live API:

* `GET /cgi-bin/gettoken` → **`errcode=0`** — the Corp ID and Secret are valid.
* `GET /cgi-bin/agent/get` → **`errcode=60020`** — `not allow to access from your ip`.
  WeCom issues tokens from anywhere but only lets **allow-listed** IPs call business
  APIs. The caller's public IP was `14.212.114.52`; add it under
  我的企业 → 企业信息 → **可信IP** in the WeCom admin console.

> **No message has ever been sent to a real customer**, and nothing has been pulled
> from the real Session Archive API. The `token` / `encoding_aes_key` callback
> credentials are still unset.

The service therefore still runs in **`mock` mode by default**: no network call to
`qyapi.weixin.qq.com` is made, and the "archive" is just a directory of JSON files
produced by the local simulator (see below).

Before you consider going live, run:

```bash
python scripts/preflight.py
```

It re-checks the credentials, tells you the exact public IP WeCom is seeing, and
exits `0` only once a live send would actually succeed.

And to verify the inbound path without WeCom:

```bash
python scripts/callback_smoke.py
```

It starts a scratch gateway in live mode with a throwaway token and
EncodingAESKey and drives it the way WeCom would — URL verification echo, an
encrypted and signed text message, and two requests that must be rejected. Needs
no credentials and no network.

---

## Run it

```bash
cd wecom-gateway

# install (from the repo root venv, or your own)
pip install -r requirements.txt        # fastapi uvicorn sqlalchemy pydantic-settings cryptography httpx

# start
python -m uvicorn app.main:app --port 8100
# or with autoreload while developing:
python -m uvicorn app.main:app --reload --port 8100
```

Then:

* Swagger UI — <http://127.0.0.1:8100/docs>
* Health — <http://127.0.0.1:8100/wecom/health> (returns `"mode": "mock"`)

Ports in this repo:

| Service | Port | Base URL |
| --- | --- | --- |
| ERP backend | 8000 | `http://127.0.0.1:8000` |
| WeCom gateway | 8100 | `http://127.0.0.1:8100` |
| Frontend (Vite) | 5173 | `http://localhost:5173` |

Database: SQLite at `wecom-gateway/data/wecom.db` by default (created on first boot).
Set `WECOM_DATABASE_URL` to a Postgres URL for anything beyond local dev.

---

## Mock-mode flow

Everything below happens **offline**. No WeCom account needed.

```
simulator/producer.py                wecom-gateway/data/mock_archive/<seq>.json
  writes fake decrypted  ───────►    wecom-gateway/data/mock_media/<sdkfileid>.png
  archive entries                                    │
                                                     │  MockWeComApi.get_chat_data(seq)
                                                     ▼
                                    app/services/archive.py  (poll loop, seq cursor)
                                                     │
                                                     ▼
                                    app/services/ingestor.py
                                      · dedupe on msgid
                                      · staff / internal-ops chats → ignored
                                      · msgtype → source_type
                                                     │
                                                     ▼
                                    app/services/identity.py  (auto-bind cascade)
                                                     │
                                                     ▼
                                    app/services/handoff.py
                                      · MockErpClient  (records calls, returns doc-0001)
                                        instead of POST http://127.0.0.1:8000/...
                                                     │
                                                     ▼
                                    wecom_message_log row: status=handed_off
```

Adapter selection is done by `app/adapters/*.py` factories (`get_wecom_api`,
`get_erp_client`, `get_storage`, `get_decryptor`). In mock mode they return
`MockWeComApi` / `MockErpClient` / `LocalStorage` / `PureCryptoDecryptor`; in live
mode they return the real HTTP/SDK-backed implementations behind the **same
interface**, so service code has no `if mock:` branches.

### Try it end to end

```bash
cd wecom-gateway

# 1. produce the fake archive stream
python -m simulator.producer --scenario all

# 2. pull + ingest in one shot (mock archive → ERP handoff).
#    The routing vars must match the simulator's actors, otherwise the
#    staff / internal-ops scenarios will be ingested as customer orders.
export WECOM_STAFF_USERIDS="ZhangSan,LiSi"
export WECOM_INTERNAL_OPS_CHAT_ID="wr-internal-ops-0001"
export WECOM_ORDER_GROUP_IDS="wrCanteenGroup001"
python -c "from app.core.database import SessionLocal; from app.services.archive import pull_once; \
db=SessionLocal(); print(pull_once(db)); db.close()"
# → {'fetched': 12, 'ingested': 9, 'skipped': 3, 'last_seq': 12}
#   3 skipped = 1 duplicate + 1 staff message + 1 internal-ops chat

# 3. start the gateway
python -m uvicorn app.main:app --port 8100 &

# 4. look at what happened
curl --noproxy '*' 'http://127.0.0.1:8100/wecom/messages?page_size=20'
```

`mock` mode fakes **WeCom**, not the ERP: `get_erp_client()` currently always
returns `HttpErpClient`, so handoff really does POST to `WECOM_ERP_BASE_URL`. Start
the ERP (`cd ../backend && uvicorn app.main:app --port 8000`) before step 2, or every
message that reaches handoff will be marked `failed` with a connection error. Known
gap — see *Tests* below.

> The sandbox/terminal proxy export (`http_proxy`) will break loopback requests.
> Use `curl --noproxy '*'` for local calls. The gateway's own HTTP clients set
> `trust_env=False` for the same reason.

The archive poller is **not** started automatically at boot — `app.main.lifespan` only
initialises the DB. Start it explicitly when you want continuous pulling:

```bash
python -c "from app.services.archive import start_poller; import time; \
t=start_poller(); time.sleep(3600); t.stop_event.set()"
```

---

## The simulator

`simulator/` is a runnable offline fake of WeCom. It writes **already-decrypted**
archive entries as `<seq>.json` into `settings.mock_archive_dir` and copies generated
media fixtures into `settings.mock_media_dir`, so `MockWeComApi.get_chat_data()` picks
them up exactly as it would real data.

```bash
cd wecom-gateway

python -m simulator.producer --list          # show every scenario
python -m simulator.producer --scenario all  # write them all, seq 1..N
python -m simulator.producer --scenario text_order
python -m simulator.producer --scenario duplicate --start-seq 100
python -m simulator.producer --scenario all --clean   # wipe first, then write
```

| Scenario | What it exercises |
| --- | --- |
| `text_order` | Chinese 1:1 text order (土豆 / 西红柿 / 鸡蛋) |
| `image_order` | PNG attachment fetched by `sdkfileid` |
| `pdf_order` | PDF attachment |
| `spreadsheet_order` | XLSX attachment |
| `duplicate` | the same `msgid` twice → second one is deduped |
| `staff_message` | from a staff userid → ignored |
| `ops_chat` | internal ops group chat → ignored |
| `unknown_sender` | nobody can be resolved → handed off with `customer_id: null` |
| `group_order` | order placed in a customer group chat |
| `reply` | message with `reply_to_msgid` set |
| `phone_match` | resolved by phone number via the ERP lookup |
| `all` | every scenario above, sequentially |

Fixtures (a real PNG, a real one-page PDF, a real minimal XLSX) are generated at setup
time with the standard library only — **no Pillow, no reportlab, no openpyxl** — and
total under 3 KB. See [`simulator/README.md`](simulator/README.md) for the entry
shape, addressing rules and full CLI.

---

## Identity resolution (the auto-bind cascade)

For every inbound message the gateway tries, in order:

1. **Pre-bound contact** — `wecom_contacts.customer_id` already set (manual bind, or
   a previous successful resolution).
2. **`[CUST:CODE]` remark tag** — the contact's display name contains e.g.
   `[CUST:CANTEEN-001]`.
3. **Phone** — the sender's phone is looked up in the ERP customer list.
4. **Group** — the message came from a group that is bound to a customer.
5. **Unresolved** — the message is *still* handed off, with `customer_id: null`, and
   an ops alert is emitted. Nothing is silently dropped.

Result is recorded on the contact as `bind_method` + `bind_confidence`, and on the
message as `bind_status`, so ops can audit and override.

---

## Environment variables

Every variable is prefixed `WECOM_`. **All credentials default to the empty string**,
so the service boots in mock mode with nothing configured. Fill them in via
environment variables or a `.env` file — **never commit real values**.

### Core

| Variable | Default | Notes |
| --- | --- | --- |
| `WECOM_MODE` | `mock` | `mock` = offline simulation, `live` = real WeCom. Anything that is not `live` is treated as mock. |
| `WECOM_APP_NAME` | `WeCom Gateway` | Swagger title |
| `WECOM_DEBUG` | `true` | |
| `WECOM_HOST` | `127.0.0.1` | |
| `WECOM_PORT` | `8100` | gateway port |
| `WECOM_DATABASE_URL` | `sqlite:///<gw>/data/wecom.db` | use Postgres outside local dev |
| `WECOM_CORS_ORIGINS` | `http://localhost:5173` | comma separated |

### WeCom credentials — **PLACEHOLDERS, fill in at go-live**

| Variable | Default | Where to get it |
| --- | --- | --- |
| `WECOM_CORP_ID` | `""` | WeCom admin → 我的企业 → 企业ID |
| `WECOM_AGENT_ID` | `""` | WeCom admin → 应用管理 → your app → AgentId |
| `WECOM_SECRET` | `""` | same screen → Secret → 查看 |
| `WECOM_TOKEN` | `""` | you choose this when configuring the callback URL |
| `WECOM_ENCODING_AES_KEY` | `""` | 43-char key, generated by WeCom when you save the callback |

### Session archive (会话存档)

| Variable | Default | Notes |
| --- | --- | --- |
| `WECOM_ARCHIVE_PRIVATE_KEY_PATH` | `""` | path to the RSA-2048 **private** key PEM whose public half you uploaded to WeCom. **PATH ONLY — never put the key material in an env var.** |
| `WECOM_ARCHIVE_SDK_PATH` | `""` | path to the official `WeWorkFinanceSdk` `.so`/`.dll` if you use `WECOM_DECRYPT_PROVIDER=sdk` |
| `WECOM_DECRYPT_PROVIDER` | `pure` | `pure` = pure-Python RSA+AES via `cryptography`; `sdk` = official C SDK via ctypes |
| `WECOM_ARCHIVE_PULL_INTERVAL` | `30` | seconds between archive polls |
| `WECOM_ARCHIVE_LIMIT` | `1000` | max entries per pull |
| `WECOM_ARCHIVE_TIMEOUT` | `5` | seconds per pull HTTP call |

### Routing

| Variable | Default | Notes |
| --- | --- | --- |
| `WECOM_STAFF_USERIDS` | `""` | comma-separated internal staff userids; their messages are **ignored** (never treated as customer orders) |
| `WECOM_ORDER_GROUP_IDS` | `""` | comma-separated group `chat_id`s that accept orders; also the default destination for ops notifications |
| `WECOM_INTERNAL_OPS_CHAT_ID` | `""` | ops group `chat_id`; conversations here are ignored, alerts are sent here |

### Storage

| Variable | Default | Notes |
| --- | --- | --- |
| `WECOM_MEDIA_DIR` | `<repo>/data/wecom` | gateway-local. The gateway serves media itself via `/wecom/media`; the ERP no longer shares disk (this repo was split out of the ERP monorepo) |
| `WECOM_MEDIA_URL_BASE` | `http://127.0.0.1:8100/wecom/media` | URL handed to the ERP in `file_url` |
| `WECOM_MOCK_ARCHIVE_DIR` | `<gw>/data/mock_archive` | where the simulator writes |
| `WECOM_MOCK_MEDIA_DIR` | `<gw>/data/mock_media` | where the simulator writes |
| `WECOM_OUTBOX_DIR` | `<gw>/data/outbox` | in mock mode, outbound messages are written here instead of being sent |

### ERP connection

| Variable | Default | Notes |
| --- | --- | --- |
| `WECOM_ERP_BASE_URL` | `http://127.0.0.1:8000` | |
| `WECOM_ERP_API_KEY` | `""` | **PLACEHOLDER** — shared key sent as `X-ERP-Service-Key` to the ERP. The ERP's own default is `dev-service-key` (its `ERP_SERVICE_KEY`), so set `WECOM_ERP_API_KEY=dev-service-key` for local dev — otherwise every handoff is rejected with **401 Not authenticated** |
| `WECOM_GATEWAY_SERVICE_KEY` | `dev-gateway-key` | **PLACEHOLDER** — secret the ERP must present as `X-Gateway-Key` on `POST /wecom/send`. The default is a dev-only value; change it. |

---

## Endpoints

All under `http://127.0.0.1:8100`. List endpoints return
`{items, total, page, page_size}`.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/wecom/health` | status, `mode`, ERP reachability, contact/message counts |
| GET | `/wecom/callback` | WeCom URL verification (echoes `echostr`) |
| POST | `/wecom/callback` | WeCom event callback (encrypted XML) |
| POST | `/wecom/archive/callback` | archive-availability notification |
| POST | `/wecom/ingest` | inject one already-decrypted archive entry (used by the simulator) |
| GET | `/wecom/messages` | message log; filters `status`, `customer_id`, `direction`, `msgid` |
| GET | `/wecom/messages/{id}` | one message |
| POST | `/wecom/messages/{id}/rehand` | retry a failed handoff |
| GET | `/wecom/contacts` | contact list; filters `customer_id`, `q` |
| PATCH | `/wecom/contacts/{external_userid}` | edit name / flags / manual `customer_id` |
| POST | `/wecom/contacts/{external_userid}/bind` | bind a contact to an ERP customer |
| GET | `/wecom/groups` | group list |
| POST | `/wecom/groups` | register a group |
| DELETE | `/wecom/groups/{chat_id}` | remove a group |
| POST | `/wecom/send` | ERP → customer/ops notification (requires `X-Gateway-Key`) |
| GET | `/wecom/outbound` | outbound log; filters `customer_id`, `template`, `status` |
| GET | `/wecom/media/{filename}` | serve a downloaded attachment |

Message statuses: `received` → `handed_off` | `ignored` | `failed` | `duplicate`.

---

## Outbound templates

The ERP sends `POST /wecom/send` with `{template, customer_id|external_userid|chat_id, locale, payload}`.
Six templates, each in `en` and `zh`:

`order_confirmed`, `needs_customer_confirm`, `parse_failed`, `out_for_delivery`,
`delivered`, `invoice_ready`.

Rendering never raises: a missing payload key renders as `-`. Destination resolution
order is **explicit `external_userid` → bound contact for `customer_id` → configured
order group → explicit `chat_id`**; if nothing resolves, the message is logged as
`skipped` rather than dropped. In mock mode the rendered text is also written to
`data/outbox/`.

---

## Tests

```bash
cd wecom-gateway
python -m pytest -q
```

The suite is hermetic: `tests/conftest.py` points `settings` and the SQLAlchemy engine
at a temporary directory and a temporary SQLite file, and rebuilds the schema for every
test — **`data/wecom.db` is never touched**.

Coverage follows the DoD in `docs/WECOM_CONTRACTS.md` §10: dedupe, staff/ops
filtering, msgtype→source_type routing, all five cascade branches, the exact §6
handoff payload shape, all six templates in both locales, destination fallback order,
callback handling, and `/wecom/health` reporting `mode=mock`.

Both gaps that used to be marked `xfail` are now fixed and covered by ordinary
assertions:

* `app/core/callback_crypto.py` — `encrypt()` used to write a 17-byte prefix while
  `decrypt()` strips 16, so every round trip was off by a byte. The prefix is now
  exactly 16 bytes and `test_encrypt_writes_a_16_byte_prefix` pins it.
* `app/services/ingestor.py` — `reply_to_msgid` is extracted, stored on the row and
  forwarded by `handoff()`, which selects `POST /api/v1/intake/wecom/reply`.

One deliberate non-gap: `get_erp_client()` always returns `HttpErpClient`, even in
mock mode. That is intentional — `mock` stubs **WeCom**, not the ERP. The point of
mock mode is to exercise the real gateway→ERP path end to end with no WeCom
credentials, so stubbing the ERP too would leave the handoff untested. Mock runs
therefore need the ERP running on `WECOM_ERP_BASE_URL`. `MockErpClient` exists for
unit tests only.

---

## Go-live checklist

Nothing below has been done or verified. Work through it in order.

**WeCom admin console**

- [ ] Enable **会话存档 (Session Archive)** for the corp and complete the
      enterprise verification WeCom requires.
- [ ] Generate an **RSA-2048 key pair**. Upload the *public* key to WeCom as the
      archive public key; keep the *private* key off the repo — put it outside the
      working tree (e.g. `/etc/wecom/archive_private.pem`, mode `0600`) and point
      `WECOM_ARCHIVE_PRIVATE_KEY_PATH` at it.
- [ ] Create/select the **self-built app** and record **Corp ID**, **Agent ID** and
      **Secret**.
- [ ] Configure the **callback URL** (`https://<host>/wecom/callback`) and let WeCom
      generate a **Token** and a 43-character **EncodingAESKey**.
- [ ] Whitelist the gateway's egress IP in the WeCom app's **可信IP** list.
- [ ] Add the archive **SDK library** to the host if you choose
      `WECOM_DECRYPT_PROVIDER=sdk`; `pure` needs only `cryptography`.

**Server / environment**

- [ ] `WECOM_MODE=live`
- [ ] `WECOM_CORP_ID`, `WECOM_AGENT_ID`, `WECOM_SECRET` — real values
- [ ] `WECOM_TOKEN`, `WECOM_ENCODING_AES_KEY` — exactly as shown in the WeCom console
- [ ] `WECOM_ARCHIVE_PRIVATE_KEY_PATH` — absolute path to the RSA private PEM
- [ ] `WECOM_STAFF_USERIDS` — every internal staff userid (otherwise staff chats get
      ingested as customer orders)
- [ ] `WECOM_INTERNAL_OPS_CHAT_ID` — the ops group for alerts
- [ ] `WECOM_SEND_ALLOWLIST` — set to your own userid for the **first** live send, so
      a mis-resolved destination is refused (`status=blocked`) instead of reaching a
      real customer. Clear it once resolution is proven.
- [ ] `WECOM_ORDER_GROUP_IDS` — every customer group that may place orders
- [ ] `WECOM_ERP_API_KEY` — real shared key; `WECOM_GATEWAY_SERVICE_KEY` — replace the
      `dev-gateway-key` default and give it to the ERP
- [ ] `WECOM_DATABASE_URL` — Postgres, not SQLite
- [ ] `WECOM_MEDIA_DIR` — on a volume the ERP can also read
- [ ] TLS in front of the gateway; WeCom requires HTTPS callbacks
- [ ] Secrets come from a secret manager / env file outside the repo; confirm
      `git status` is clean and no `.env` with real values is committed

**Verification before trusting it**

- [ ] `GET /wecom/health` reports `mode=live`, `erp_reachable=true`,
      `archive_enabled=true`
- [ ] Send one real 1:1 text message; confirm it appears in `/wecom/messages` with
      `status=handed_off` and the **correct** `customer_id`
- [ ] Send the same message again; confirm `status=duplicate` and that the ERP
      received exactly one intake job
- [ ] Send an image and a PDF; confirm `source_type` and that the ERP can fetch
      `file_url` **and** read `file_path` off disk
- [ ] Send from a staff account; confirm `status=ignored`
- [ ] Send from an unrecognised number; confirm it is handed off with
      `customer_id: null` and that ops got an alert
- [ ] Trigger `POST /wecom/send` from the ERP **with `WECOM_SEND_ALLOWLIST` set to your
      own userid**; confirm you receive it and `/wecom/outbound` shows `status=sent`
- [ ] Send to a second, non-allowlisted destination; confirm `status=blocked` and that
      nothing was delivered — this proves the gate before you point it at customers
- [ ] Clear `WECOM_SEND_ALLOWLIST` only after destination resolution is proven
- [ ] Confirm the callback signature check rejects a tampered request
- [ ] `python scripts/preflight.py` exits `0` (credentials valid **and** IP allow-listed)
- [ ] `python scripts/callback_smoke.py` exits `0`
- [ ] Save the callback URL in the WeCom console and confirm WeCom accepts it. This is
      the first real test of the signature scheme — mock mode skips it entirely, and
      the four-value `msg_signature` is exactly the kind of thing that only shows up
      here.

---

## Layout

```
wecom-gateway/
├── app/
│   ├── main.py                 FastAPI app, CORS, /wecom/media
│   ├── api/                    routers: health, callback, messages, contacts, groups, send
│   ├── core/                   config, database, callback_crypto, security
│   ├── adapters/               wecom_api, erp_client, storage, decrypt (mock|live)
│   ├── services/               archive, ingestor, identity, handoff, outbound, templates
│   ├── models/wecom.py         SQLAlchemy tables
│   └── schemas/wecom.py        Pydantic v2 models (the wire contract)
├── scripts/                    preflight.py — live-readiness check (read-only)
│                               callback_smoke.py — inbound path vs. the real crypto scheme
├── simulator/                  offline WeCom fake + generated fixtures
├── tests/                      pytest suite (hermetic, temp SQLite)
└── data/                       wecom.db, mock_archive/, mock_media/, outbox/
```

**Do not** add WeCom-specific logic to the ERP (`food2`) repo beyond the thin
console pages under its `frontend/src/pages/wecom/`. The gateway owns WeCom;
the ERP owns orders.
