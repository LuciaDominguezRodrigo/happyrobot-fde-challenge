"""Client for the FMCSA QCMobile API -- carrier authority verification by MC number.

Public REST API: https://mobile.fmcsa.dot.gov/qc/services/carriers/{mc}?webKey=...

NOTE: this endpoint returned HTTP 403 for every request tested from the FDE
dev network (corporate egress IP), independent of path or webKey -- almost
certainly a WAF blocking that egress range rather than a credential problem.
This client is written against FMCSA's documented response shape and must be
smoke-tested from the actual deployment environment before go-live.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import httpx

FMCSA_BASE_URL = "https://mobile.fmcsa.dot.gov/qc/services/carriers"

# Statuses considered "active operating authority" per the requirement to
# check for active authority, not just carrier existence.
_ACTIVE_STATUSES = {"A", "ACTIVE"}


class AuthorityStatus(Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    NOT_FOUND = "not_found"
    LOOKUP_FAILED = "lookup_failed"  # transport/API fault -- distinct from a real "no authority"


@dataclass
class CarrierAuthority:
    mc_number: str
    status: AuthorityStatus
    legal_name: Optional[str] = None
    dot_number: Optional[str] = None
    raw: Optional[dict] = None


class FmcsaClient:
    def __init__(self, web_key: str, timeout: float = 6.0, max_attempts: int = 3):
        self._web_key = web_key
        self._timeout = timeout
        self._max_attempts = max_attempts

    def check_authority(self, mc_number: str) -> CarrierAuthority:
        mc_number = mc_number.strip().upper().removeprefix("MC-").removeprefix("MC")
        url = f"{FMCSA_BASE_URL}/docket-number/{mc_number}"
        last_error: Optional[Exception] = None

        for attempt in range(self._max_attempts):
            try:
                resp = httpx.get(url, params={"webKey": self._web_key}, timeout=self._timeout)
            except httpx.TimeoutException as exc:
                last_error = exc
                if attempt < self._max_attempts - 1:
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                return CarrierAuthority(mc_number=mc_number, status=AuthorityStatus.LOOKUP_FAILED)

            if resp.status_code == 404:
                return CarrierAuthority(mc_number=mc_number, status=AuthorityStatus.NOT_FOUND)
            if resp.status_code >= 500:
                last_error = RuntimeError(f"FMCSA {resp.status_code}")
                if attempt < self._max_attempts - 1:
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                return CarrierAuthority(mc_number=mc_number, status=AuthorityStatus.LOOKUP_FAILED)
            if resp.status_code != 200:
                # Includes 403 (WAF/auth issue) -- not retried, this is a
                # configuration problem, not a transient fault.
                return CarrierAuthority(mc_number=mc_number, status=AuthorityStatus.LOOKUP_FAILED, raw={"http_status": resp.status_code})

            data = resp.json()
            content = (data.get("content") or [{}])[0] if isinstance(data.get("content"), list) else data.get("content") or {}
            carrier = content.get("carrier", content)
            if not carrier:
                return CarrierAuthority(mc_number=mc_number, status=AuthorityStatus.NOT_FOUND, raw=data)

            allowed_to_operate = str(carrier.get("allowedToOperate", "")).strip().upper()
            status = AuthorityStatus.ACTIVE if allowed_to_operate in _ACTIVE_STATUSES else AuthorityStatus.INACTIVE
            return CarrierAuthority(
                mc_number=mc_number,
                status=status,
                legal_name=carrier.get("legalName"),
                dot_number=str(carrier.get("dotNumber")) if carrier.get("dotNumber") else None,
                raw=data,
            )

        return CarrierAuthority(mc_number=mc_number, status=AuthorityStatus.LOOKUP_FAILED)
