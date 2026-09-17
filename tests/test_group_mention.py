"""App-callback group attribution: carry `RoomId` through as `chat_id`.

When the self-built WeCom app is added to a group and a user @-mentions it,
WeCom delivers the message with a `RoomId` field. The gateway must carry that
through so the message is attributed to the GROUP (chat_id == roomid) and routed
as a group message — not mis-classified as a 1:1 DM (which would resolve the
wrong counterparty).

This is the APP callback, NOT the 智能机器人 (Smart Robot) callback. The Smart
Robot was retired: it cannot be added to external customer groups, and its
conversations are not archivable at all. The `@bot` in the fixtures below is
example message text and nothing more.

The app callback itself cannot be configured on a PaaS host either — the console
enforces domain-entity (备案主体) verification, which a `*.up.railway.app`
hostname can never pass. It is kept for a future owned domain.
"""
from __future__ import annotations

from app.models.wecom import WeComMessageLog


def test_app_callback_captures_roomid_for_group_mention(client, mock_erp, db):
    msg = {
        "MsgType": "text",
        "MsgId": "GRP-MSG-1",
        "FromUserName": "externalUserA",
        "RoomId": "grp-001",
        "Content": "下两箱土豆 @bot",
    }
    res = client.post("/wecom/callback", json=msg)
    assert res.status_code == 200

    logged = db.query(WeComMessageLog).filter_by(msgid="GRP-MSG-1").one_or_none()
    assert logged is not None, "group message should be stored"
    # Group attribution: chat_id comes from RoomId, NOT empty (which would mean 1:1)
    assert logged.chat_id == "grp-001"


def test_app_callback_without_roomid_stays_one_to_one(client, mock_erp, db):
    """Sanity: a plain 1:1 DM (no RoomId) must still be attributed as 1:1."""
    msg = {
        "MsgType": "text",
        "MsgId": "DM-MSG-1",
        "FromUserName": "externalUserB",
        "Content": "你好",
    }
    res = client.post("/wecom/callback", json=msg)
    assert res.status_code == 200

    logged = db.query(WeComMessageLog).filter_by(msgid="DM-MSG-1").one_or_none()
    assert logged is not None
    assert logged.chat_id in (None, "")
