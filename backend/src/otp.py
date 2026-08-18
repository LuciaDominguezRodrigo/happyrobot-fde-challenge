"""OTP issuance and verification for carrier identity confirmation.

Hard requirements this module exists to satisfy:
- FMCSA authority check alone is not sufficient; an OTP must be sent to the
  carrier's email or SMS and verified before load matching proceeds.
- The flow must resist social engineering -- there is no code path that
  returns the OTP value to a caller, logs it anywhere the voice agent's
  prompt could read it back, or allows a "just tell me the code" bypass.
  The agent's own conversation policy is the other half of this control;
  this module only guarantees the code never leaves the delivery channel.

Delivery (SMS/email send) is stubbed behind `NotificationSender` -- swap in
a real Twilio/SES-backed implementation without touching verification logic.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Protocol

OTP_LENGTH = 6
OTP_TTL_SECONDS = 5 * 60
MAX_VERIFY_ATTEMPTS = 3


class NotificationSender(Protocol):
    def send(self, destination: str, message: str) -> None: ...


class LoggingNotificationSender:
    """Stub sender for local/dev use. Never let this ship to production --
    it exists so the workflow is demoable without real SMS/email credentials.

    The print here is a server console line, not an API response -- it's the
    out-of-band delivery channel itself (standing in for the SMS/email that a
    real integration would send), readable only by whoever operates this
    server, never by the voice agent or its prompt."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send(self, destination: str, message: str) -> None:
        self.sent.append((destination, message))
        print(f"[DEV OTP DELIVERY] to {destination}: {message}")


@dataclass
class _OtpRecord:
    code_hash: str
    salt: str
    expires_at: float
    attempts_remaining: int
    verified: bool = False


class OtpService:
    def __init__(self, sender: NotificationSender):
        self._sender = sender
        self._records: dict[str, _OtpRecord] = {}

    @staticmethod
    def _hash(code: str, salt: str) -> str:
        return hmac.new(salt.encode(), code.encode(), hashlib.sha256).hexdigest()

    def send_otp(self, call_id: str, destination: str) -> None:
        code = "".join(secrets.choice("0123456789") for _ in range(OTP_LENGTH))
        salt = secrets.token_hex(16)
        self._records[call_id] = _OtpRecord(
            code_hash=self._hash(code, salt),
            salt=salt,
            expires_at=time.monotonic() + OTP_TTL_SECONDS,
            attempts_remaining=MAX_VERIFY_ATTEMPTS,
        )
        self._sender.send(destination, f"Your HappyRobot carrier verification code is {code}. It expires in 5 minutes.")

    def verify_otp(self, call_id: str, submitted_code: str) -> bool:
        record = self._records.get(call_id)
        if record is None or record.verified:
            return False
        if time.monotonic() > record.expires_at:
            return False
        if record.attempts_remaining <= 0:
            return False
        record.attempts_remaining -= 1
        if hmac.compare_digest(record.code_hash, self._hash(submitted_code.strip(), record.salt)):
            record.verified = True
            return True
        return False

    def is_verified(self, call_id: str) -> bool:
        record = self._records.get(call_id)
        return bool(record and record.verified)
