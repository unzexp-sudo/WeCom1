"""Gateway settings.

Every WeCom credential has an empty default so the service starts, imports and
runs fully in `mock` mode with no secrets configured. Real credentials are
supplied later purely through environment variables — never hardcoded.
"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# app/core/config.py -> parents[0]=core, [1]=app, [2]=repo root (WeCom1)
GATEWAY_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = GATEWAY_DIR


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WECOM_",
        env_file=(".env", str(REPO_ROOT / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "WeCom Gateway"
    # "mock" = fully offline simulation, "live" = real WeCom APIs
    mode: str = "mock"
    debug: bool = True
    host: str = "127.0.0.1"
    port: int = 8100

    # --- Database -------------------------------------------------------------
    database_url: str = f"sqlite:///{GATEWAY_DIR / 'data' / 'wecom.db'}"

    # Durability escape hatch. The container filesystem is ephemeral, so the
    # default SQLite file is destroyed on every redeploy — taking the contact
    # bindings, the message log and the dedupe record with it. In production we
    # refuse to start on SQLite unless this is explicitly acknowledged.
    allow_ephemeral_database: bool = False

    # --- WeCom app credentials (placeholders; filled in later via env) --------
    corp_id: str = ""
    agent_id: str = ""
    secret: str = ""
    token: str = ""
    encoding_aes_key: str = ""

    # --- Session archive ------------------------------------------------------
    # The 会话内容存档 (Session Archive) secret, from 管理工具 → 聊天内容存档.
    # This is DISTINCT from WECOM_SECRET (the self-built app secret used for
    # outbound send). Both the archive access token (for the `message/getchatdata`
    # pull — see `ARCHIVE_PULL_PATH` in app/adapters/wecom_api.py) and the finance
    # SDK `Init()` require THIS secret, not the app secret.
    #
    # NOTE: an earlier version of this comment claimed an empty value "falls
    # back to WECOM_SECRET so single-secret setups still boot". That fallback
    # was never implemented, and implementing it would be wrong anyway — the
    # app secret is not accepted for msgaudit. Leave this EMPTY only if you do
    # not intend to use the archive; otherwise set it, or every archive pull
    # fails with "WECOM_CORP_ID / WECOM_ARCHIVE_SECRET are not configured".
    archive_secret: str = ""
    archive_private_key_path: str = ""
    # Container platforms hand us env vars, not files, and `PureCryptoDecryptor`
    # reads a PATH. This carries the PEM as base64 so the gateway can materialise
    # it to a 0600 file at boot (see `archive.materialize_private_key`). Prefer
    # `archive_private_key_path` on a host where you can place the file directly;
    # this exists so a Railway/Heroku-style deploy has *some* way to work.
    archive_private_key_b64: str = ""
    # Path to `libWeWorkFinanceSdk_C.so`. Leave EMPTY to let the gateway fetch the
    # official library itself into `vendor/` at boot (see
    # `services/sdk_bootstrap.py`) — which is the only practical option on a
    # container, where the filesystem is ephemeral and there is no shell. Set it
    # explicitly when you ship the library yourself (e.g. baked into an image).
    archive_sdk_path: str = ""
    # Fetch the SDK at boot when `archive_sdk_path` is unset or the file is
    # missing. Pinned to a vendor URL and verified by md5, so this is a supply
    # of a known artefact rather than a live dependency on the network.
    archive_sdk_autofetch: bool = True
    # "pure" = pure-Python RSA/AES via `cryptography`; "sdk" = official C SDK
    decrypt_provider: str = "pure"
    # Run the vendor SDK in a throwaway child process instead of in-process.
    # DEFAULT TRUE, because the library does not fail politely: handed input it
    # cannot parse it aborts the process (`free(): invalid pointer`, exit 133), and
    # a native abort is uncatchable in Python. In-process that costs the whole
    # gateway — which then crash-loops and never serves a request — rather than the
    # one unreadable entry. Set false only to debug the binding itself.
    sdk_isolate: bool = True

    # --- Routing --------------------------------------------------------------
    staff_userids: str = ""
    order_group_ids: str = ""
    internal_ops_chat_id: str = ""

    # --- Ingest scope ---------------------------------------------------------
    # 会话内容存档 returns EVERY conversation in the corp — internal chats,
    # 1:1s, groups that have nothing to do with orders. The only other inbound
    # filter is `staff_userids`, which is a deny-list: it can only exclude
    # people you remembered to list.
    #
    # When true, a message is ingested only if its `chat_id` is in
    # `order_group_ids`. Everything else is recorded as `ignored` with a reason,
    # so nothing vanishes silently.
    #
    # DEFAULT FALSE — existing behaviour, and the existing tests, are untouched.
    #
    # Deliberate failure mode: if this is true but `order_group_ids` is EMPTY,
    # the gate does NOT engage (it ingests everything) and `/wecom/health`
    # reports a warning. A noisy queue is recoverable; an order dropped by a
    # misconfiguration is not. Failing open is the only defensible direction
    # for order intake.
    #
    # Trade-off to be aware of: this is group-only. A customer who orders in a
    # 1:1 chat has no `chat_id` and would be ignored. Your model is that orders
    # arrive in group chats (see docs/WECOM_CONTRACTS.md), so this matches it —
    # but it is a real behaviour change, which is why it is opt-in.
    ingest_only_order_groups: bool = False

    # --- Blast-radius control -------------------------------------------------
    # Comma-separated external_userids / chat_ids. When non-empty, a *live* send
    # to anything not on this list is refused and logged as `blocked` instead of
    # delivered. Empty means no restriction.
    #
    # This exists for the first live test. Destination resolution is a five-step
    # cascade over data we have never seen from a real corp; if it resolves to
    # the wrong person, the first thing they ever receive from us is a wrong
    # order confirmation. Set this to your own userid for the first send, watch
    # `/wecom/outbound`, then clear it once resolution is proven.
    #
    # Mock mode is deliberately exempt: nothing is delivered there anyway, and
    # gating it would break the end-to-end mock flow the tests rely on.
    send_allowlist: str = ""

    # --- Archive polling ------------------------------------------------------
    archive_pull_interval: int = 30
    archive_limit: int = 1000
    archive_timeout: int = 5

    # --- Storage --------------------------------------------------------------
    # Shared with the ERP on purpose: the ERP reads intake files straight off disk.
    media_dir: str = str(GATEWAY_DIR / "data" / "wecom")
    media_url_base: str = "http://127.0.0.1:8100/wecom/media"
    mock_archive_dir: str = str(GATEWAY_DIR / "data" / "mock_archive")
    mock_media_dir: str = str(GATEWAY_DIR / "data" / "mock_media")
    outbox_dir: str = str(GATEWAY_DIR / "data" / "outbox")

    # --- ERP connection -------------------------------------------------------
    erp_base_url: str = "http://127.0.0.1:8000"
    # Dev placeholder matching the ERP's ERP_SERVICE_KEY default. Replace both
    # together before any real deployment.
    erp_api_key: str = "dev-service-key"
    # Shared secret the ERP must present on POST /wecom/send
    gateway_service_key: str = "dev-gateway-key"

    # The WeCom console calls this service directly from the browser (it is not
    # behind the Vite /api proxy), so the dev origin must be allowed or every
    # call dies in a preflight the UI only reports as "gateway unreachable".
    # Both loopback spellings and the port Vite falls back to when 5173 is
    # taken are listed because which one the browser sends as `Origin` depends
    # on how the developer opened the page, not on how we configured anything.
    cors_origins: str = (
        "http://localhost:5173,http://127.0.0.1:5173,"
        "http://localhost:5174,http://127.0.0.1:5174"
    )

    # --- Derived helpers ------------------------------------------------------

    @property
    def is_mock(self) -> bool:
        return self.mode.strip().lower() != "live"

    @property
    def is_live(self) -> bool:
        return not self.is_mock

    def staff_list(self) -> list[str]:
        return [s.strip() for s in self.staff_userids.split(",") if s.strip()]

    def order_group_list(self) -> list[str]:
        return [s.strip() for s in self.order_group_ids.split(",") if s.strip()]

    def send_allowlist_set(self) -> set[str]:
        return {s.strip() for s in self.send_allowlist.split(",") if s.strip()}

    @property
    def gateway_service_key_is_default(self) -> bool:
        """True while the shared secret is still the published placeholder.

        This key guards POST /wecom/send and POST /wecom/archive/pull. Its default
        is committed to the repo and printed in .env.example, so a deployment that
        never overrode it is "protected" by a value anyone who can read the repo
        already knows. The comparison reads the field's own default rather than
        repeating the literal, so it stays correct if the placeholder ever changes.
        """
        default = type(self).model_fields["gateway_service_key"].default
        return (self.gateway_service_key or "").strip() == (default or "").strip()

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def media_path(self, *parts: str) -> Path:
        target = Path(self.media_dir).joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def outbox_path(self, *parts: str) -> Path:
        target = Path(self.outbox_dir).joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target


settings = Settings()
