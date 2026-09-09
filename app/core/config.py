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

    # --- WeCom app credentials (placeholders; filled in later via env) --------
    corp_id: str = ""
    agent_id: str = ""
    secret: str = ""
    token: str = ""
    encoding_aes_key: str = ""

    # --- Smart-bot (智能机器人) credentials -----------------------------------
    # Same crypto scheme as the self-built app, but with a separate Token and
    # EncodingAESKey the admin sets in the smart-bot's "URL 回调" config. Empty
    # defaults keep the service mock-safe; URL verification only enforces
    # signature + decryption when both are configured (matching the self-built
    # app callback behaviour — see app/api/callback.py).
    bot_token: str = ""
    bot_encoding_aes_key: str = ""

    # --- Session archive ------------------------------------------------------
    # The 会话内容存档 (Session Archive) secret, from 管理工具 → 聊天内容存档.
    # This is DISTINCT from WECOM_SECRET (the self-built app secret used for
    # outbound send). Both the `msgaudit/get_chat_data` access token and the
    # finance SDK `Init()` require THIS secret, not the app secret. Empty →
    # fall back to WECOM_SECRET so single-secret setups still boot.
    archive_secret: str = ""
    archive_private_key_path: str = ""
    archive_sdk_path: str = ""
    # "pure" = pure-Python RSA/AES via `cryptography`; "sdk" = official C SDK
    decrypt_provider: str = "pure"

    # --- Routing --------------------------------------------------------------
    staff_userids: str = ""
    order_group_ids: str = ""
    internal_ops_chat_id: str = ""

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
