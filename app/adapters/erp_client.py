"""ERP handoff client — real HTTP and mock behind one interface.

The Gateway hands every customer message to the ERP at
`POST {ERP_BASE}/api/v1/intake/wecom` and lets the ERP do all parsing.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol

import httpx

from app.core.config import settings

logger = logging.getLogger("wecom.erp")


class ErpClientError(RuntimeError):
    pass


class ErpClient(Protocol):
    def intake_wecom(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def intake_reply(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def find_customer(
        self, *, code: str | None = None, phone: str | None = None
    ) -> dict[str, Any] | None: ...

    def health(self) -> bool: ...


def _client(timeout: float = 30.0) -> httpx.Client:
    # trust_env=False: the sandbox proxy env vars break loopback calls.
    return httpx.Client(trust_env=False, timeout=timeout)


class HttpErpClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None) -> None:
        self.base_url = (base_url or settings.erp_base_url).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.erp_api_key

    def _headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["X-ERP-Service-Key"] = self.api_key
        if idempotency_key:
            h["Idempotency-Key"] = idempotency_key
        return h

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            with _client() as c:
                r = c.post(
                    url,
                    json=payload,
                    headers=self._headers(payload.get("msgid")),
                )
        except httpx.HTTPError as exc:
            raise ErpClientError(f"ERP unreachable at {url}: {exc}") from exc

        if r.status_code >= 400:
            raise ErpClientError(f"ERP {path} returned {r.status_code}: {r.text[:500]}")
        try:
            return r.json()
        except ValueError as exc:
            raise ErpClientError(f"ERP {path} returned non-JSON: {r.text[:200]}") from exc

    def intake_wecom(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post("/api/v1/intake/wecom", payload)

    def intake_reply(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post("/api/v1/intake/wecom/reply", payload)

    def find_customer(
        self, *, code: str | None = None, phone: str | None = None
    ) -> dict[str, Any] | None:
        """Resolve an ERP customer by code or phone (auto-bind cascade)."""
        params: dict[str, str] = {}
        if code:
            params["code"] = code
        if phone:
            params["phone"] = phone
        if not params:
            return None
        url = f"{self.base_url}/api/v1/intake/wecom/lookup-customer"
        try:
            with _client(timeout=10.0) as c:
                r = c.get(url, params=params, headers=self._headers())
        except httpx.HTTPError as exc:
            raise ErpClientError(f"ERP lookup-customer unreachable: {exc}") from exc
        if r.status_code != 200:
            return None
        try:
            data = r.json()
        except ValueError:
            return None
        return data.get("customer") if data.get("found") else None

    def health(self) -> bool:
        try:
            with _client(timeout=5.0) as c:
                r = c.get(f"{self.base_url}/api/health")
            return r.status_code == 200
        except httpx.HTTPError:
            return False


class MockErpClient:
    """Offline stand-in used by gateway tests. Records every handoff."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._seq = 0

    def _fake(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._seq += 1
        self.calls.append((kind, payload))
        return {
            "document_id": f"doc-{self._seq:04d}",
            "job_id": f"job-{self._seq:04d}",
            "customer_id": payload.get("customer_id"),
            "status": "queued",
            "duplicate": False,
        }

    def intake_wecom(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._fake("intake", payload)

    def intake_reply(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._fake("reply", payload)

    def find_customer(
        self, *, code: str | None = None, phone: str | None = None
    ) -> dict[str, Any] | None:
        self.calls.append(("lookup", {"code": code, "phone": phone}))
        # Offline tests seed bindings explicitly; default to "not found".
        found = getattr(self, "customer_lookup_result", None)
        return found

    def health(self) -> bool:
        return True


def get_erp_client() -> ErpClient:
    """Resolve the ERP client.

    Deliberately NOT switched on `settings.is_mock`: mock mode means "no WeCom
    credentials" (mock archive + mock WeCom API), NOT "no ERP". The ERP is a
    local service that is normally reachable, and the whole point of mock mode
    is to exercise the real gateway→ERP path without WeCom credentials.
    Returning MockErpClient here would silently fake every handoff.
    """
    return HttpErpClient()
