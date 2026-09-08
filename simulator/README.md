# WeCom simulator

An offline fake of WeCom. It writes **already-decrypted** Session-Archive
entries to disk so the gateway's `MockWeComApi` can pick them up and the whole
ingest chain (dedupe → bind → download media → hand off to the ERP) can run with
no WeCom account and no network.

```
simulator/
  __init__.py
  producer.py     scenario definitions + CLI
  media.py        tiny PNG / PDF / XLSX generators (stdlib only)
  fixtures/       the generated sample files (~2.4 KB in total)
  README.md
```

Two directories are written:

| what | where | read by |
|---|---|---|
| `<seq>.json` archive entries | `WECOM_MOCK_ARCHIVE_DIR` → `data/mock_archive/` | `MockWeComApi.get_chat_data()` |
| `<sdkfileid>.<ext>` media | `WECOM_MOCK_MEDIA_DIR` → `data/mock_media/` | `MockWeComApi.download_media()` |

## Run

Always from the `wecom-gateway/` directory:

```bash
cd wecom-gateway

python -m simulator.producer --list                 # show every scenario
python -m simulator.producer --scenario all         # write them all (seq 1..12)
python -m simulator.producer --scenario text_order  # just one
python -m simulator.producer --scenario duplicate --clean   # wipe *.json first
python -m simulator.producer --scenario all --start-seq 100 # continue an existing stream
```

`--clean` deletes existing `*.json` in the archive dir first. Without it a
re-run is still idempotent: files are named `<seq>.json`, so the same scenario
overwrites its own files.

Useful overrides: `--archive-dir`, `--media-dir`.

Then start the gateway and pull:

```bash
python -m uvicorn app.main:app --port 8100
curl --noproxy '*' -X POST http://127.0.0.1:8100/wecom/archive/callback   # triggers one pull
curl --noproxy '*' "http://127.0.0.1:8100/wecom/messages"                # see what landed
```

## Scenarios

| scenario | what it proves |
|---|---|
| `text_order` | 1:1 Chinese text order from a known contact. The remark is `第一食堂 李阿姨 [CUST:CANTEEN-001]`, so the `[CUST:…]` auto-bind rule can resolve it. |
| `image_order` | `msgtype=image`, `sdkfileid=mockfile-order-png-0001` → fixture PNG → `source_type=image`. |
| `pdf_order` | `msgtype=file` with `order-sample.pdf` → `source_type=pdf`. |
| `spreadsheet_order` | `msgtype=file` with `order-sample.xlsx` → `source_type=excel`. |
| `duplicate` | the **same** `msgid` at seq N and N+1 (a WeCom replay). The second must be recorded as `duplicate` and must not trigger a second ERP call. |
| `staff_message` | sender `ZhangSan`, `is_staff=true` → must be `ignored`, no ERP call. |
| `ops_chat` | message inside `wr-internal-ops-0001` → must be `ignored`. |
| `unknown_sender` | no remark tag, no phone, no group → handed off with `customer_id: null` (plus an ops alert). |
| `group_order` | order posted in `wrCanteenGroup001` → resolved through the group→customer binding. |
| `reply` | `reply_to_msgid=wmMsgText0001` → lands on the same customer via `/intake/wecom/reply`. |
| `phone_match` | resolvable only by phone `13800138000` (auto-bind step 3). |

### Settings you will want for the "ignore" scenarios

`staff_message` / `ops_chat` are only skipped when the gateway knows those ids,
so add them to your env (or `wecom-gateway/.env`):

```bash
WECOM_STAFF_USERIDS=ZhangSan,LiSi
WECOM_INTERNAL_OPS_CHAT_ID=wr-internal-ops-0001
WECOM_ORDER_GROUP_IDS=wrCanteenGroup001
```

The entries themselves also carry `is_staff`, so an implementation that trusts
the payload works too — the env vars are the belt to that braces.

## Entry shape

One JSON file per message. Both a flat, gateway-friendly set of keys and the
nested WeCom objects are present, so `normalize_entry()` can read either:

```json
{
  "msgid": "wmMsgText0001",
  "seq": 1,
  "msgtime": 1787000000000,
  "received_at": "2026-09-07T09:00:00+08:00",
  "msgtype": "text",
  "from": "wmExtCanteen001",
  "tolist": ["wmExtCanteen001"],
  "roomid": null,
  "external_userid": "wmExtCanteen001",
  "chat_id": null,
  "sender_userid": "wmExtCanteen001",
  "text": "明天要：土豆 20斤，西红柿 10斤，鸡蛋 5斤。送到第一食堂后门，谢谢！",
  "content": "同上",
  "sdkfileid": null,
  "filename": null,
  "md5": null,
  "image": null,
  "file": null,
  "reply_to_msgid": null,
  "name": "第一食堂 李阿姨 [CUST:CANTEEN-001]",
  "is_staff": false,
  "remark": "第一食堂 李阿姨 [CUST:CANTEEN-001]",
  "phone": null
}
```

For attachments the flat `sdkfileid` / `filename` / `md5` and the nested
`image` / `file` objects carry the same values.

### Addressing

`app/services/ingestor.py:normalize_entry()` reads the counterparty of a 1:1
chat from `tolist[0]` and the author from `from`. In a mock 1:1 conversation
the customer is both, so the simulator puts the **same** `external_userid` in
`from` and `tolist[0]`; that keeps an entry correct under either reading of
WeCom's `from`/`tolist` semantics. Group messages set `roomid` (and `chat_id`),
in which case `external_userid` is `null` and resolution runs through
`wecom_groups`.

`name` matters: it is what `upsert_contact()` stores, so it is where the
`[CUST:…]` remark tag has to live for the auto-bind cascade to fire.

`sdkfileid` values contain no dots on purpose: `MockWeComApi.download_media()`
globs on `Path(sdkfileid).stem`, and the copied media file is named
`<sdkfileid>.<ext>` (e.g. `mockfile-order-pdf-0001.pdf`).

## Fixtures

`media.py` builds the three sample files with the standard library only —
no Pillow, no reportlab, no openpyxl:

* `order-sample.png` — 8×8 truecolour PNG, valid CRCs (74 B)
* `order-sample.pdf` — one A4 page, Helvetica text, valid xref (697 B)
* `order-sample.xlsx` — minimal OOXML workbook, inline strings (1.6 KB)

`ensure_fixture_files()` regenerates them into `simulator/fixtures/`; the
producer copies them into `data/mock_media/`. They are intentionally content-free
— the gateway only stores and forwards them, the ERP does all parsing.
