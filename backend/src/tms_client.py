"""Client for the HappyRobot Legacy TMS (LTMS) line-oriented TCP protocol.

Protocol reference: HR-LTMS-PR-001 (FORM-9100 REV 1.0).

Key protocol facts this client is built around:
- One request per connection; server closes after writing the response.
- A successful response is zero or more "|"-delimited record lines followed
  by a literal "END" line. An error response is a single "ERR|CODE:..|MSG:.." line.
- Faults (timeout, partial response, malformed response, delayed termination)
  are never signaled -- the only reliable success marker is the END line.
  Anything else (EOF, read timeout, malformed line) is treated as a fault
  and is safe to retry, since the server never partially applies a write
  fault to LOAD_QUERY/LOAD_GET (read-only). LOAD_BOOK is the exception --
  see BookOutcome.
"""
from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

LINE_TERMINATOR = "\r\n"
MAX_FRAME_SIZE = 4096

class LtmsFault(Exception):
    """Transport-level fault: timeout, partial/malformed response, or similar.

    Safe to retry for read-only commands. Callers doing LOAD_BOOK must treat
    a fault as ambiguous (the write may have landed server-side) rather than
    as a plain failure.
    """


class LtmsApplicationError(Exception):
    """An authoritative ERR response from the server. Do not retry."""

    def __init__(self, code: str, msg: str):
        super().__init__(f"{code}: {msg}")
        self.code = code
        self.msg = msg


class BookOutcome(Enum):
    BOOKED = "booked"
    ALREADY_BOOKED_CONFIRMED = "already_booked_confirmed"  # genuine dup attempt
    ALREADY_BOOKED_AMBIGUOUS = "already_booked_ambiguous"  # our own retry may have booked it


@dataclass
class LtmsResult:
    records: list[dict]


@dataclass
class BookResult:
    outcome: BookOutcome
    load_id: str
    booking_ref: Optional[str] = None
    timestamp: Optional[str] = None


@dataclass
class LtmsClientConfig:
    host: str
    port: int
    token: str
    connect_timeout: float = 5.0
    # Read timeout per attempt. Server's own idle timeout is 30s; we use a
    # much shorter window so a stalled operational call fails fast and gets
    # retried, rather than tying up a live voice call for 30s.
    read_timeout: float = 8.0
    max_attempts: int = 3
    backoff_base: float = 0.4


class LtmsClient:
    def __init__(self, config: LtmsClientConfig):
        self._config = config

    # ---- low level -------------------------------------------------

    def _build_request(self, cmd: str, fields: dict[str, str]) -> bytes:
        parts = [f"CMD:{cmd}", f"AUTH:{self._config.token}"]
        for key, value in fields.items():
            value = str(value)
            if "|" in value or "\r" in value or "\n" in value:
                raise ValueError(f"field {key!r} value contains a reserved character: {value!r}")
            parts.append(f"{key}:{value}")
        line = "|".join(parts) + LINE_TERMINATOR
        payload = line.encode("ascii")
        if len(payload) > MAX_FRAME_SIZE:
            raise ValueError("request exceeds max frame size")
        return payload

    def _read_line(self, sock: socket.socket, buf: bytearray, deadline: float) -> str:
        """Read a single \\r\\n-terminated line from sock/buf, respecting deadline."""
        while True:
            idx = buf.find(b"\r\n")
            if idx != -1:
                line = bytes(buf[:idx])
                del buf[: idx + 2]
                if len(line) > MAX_FRAME_SIZE:
                    raise LtmsFault("line exceeds max frame size")
                return line.decode("ascii", errors="strict")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LtmsFault("timeout waiting for line")
            sock.settimeout(remaining)
            try:
                chunk = sock.recv(4096)
            except socket.timeout as exc:
                raise LtmsFault("read timeout") from exc
            except OSError as exc:
                raise LtmsFault(f"socket error: {exc}") from exc
            if not chunk:
                raise LtmsFault("connection closed before END (partial response)")
            buf.extend(chunk)
            if len(buf) > MAX_FRAME_SIZE:
                raise LtmsFault("unterminated line exceeds max frame size")

    @staticmethod
    def _parse_record_line(line: str) -> dict:
        """Parse a pipe-delimited record line. Some lines (ECHO) lead with a
        bare tag token instead of a KEY:VALUE pair -- stored under "_TAG"."""
        record = {}
        tokens = line.split("|")
        for i, token in enumerate(tokens):
            if ":" not in token:
                if i == 0:
                    record["_TAG"] = token
                    continue
                raise LtmsFault(f"malformed record token: {token!r}")
            key, _, value = token.partition(":")
            record[key] = value.rstrip(" ")
        return record

    def _request_once(self, cmd: str, fields: dict[str, str]) -> LtmsResult:
        cfg = self._config
        payload = self._build_request(cmd, fields)
        deadline = time.monotonic() + cfg.read_timeout

        with socket.create_connection((cfg.host, cfg.port), timeout=cfg.connect_timeout) as sock:
            sock.sendall(payload)
            buf = bytearray()

            first_line = self._read_line(sock, buf, deadline)
            if first_line.startswith("ERR|"):
                record = self._parse_record_line(first_line[len("ERR|"):])
                code = record.get("CODE", "UNKNOWN")
                msg = record.get("MSG", "")
                raise LtmsApplicationError(code, msg)

            records = []
            line = first_line
            while line != "END":
                records.append(self._parse_record_line(line))
                line = self._read_line(sock, buf, deadline)
            return LtmsResult(records=records)
            # `with` closes the socket immediately here -- we never wait
            # around for the server's own close (handles delayed termination).

    def _request_with_retry(self, cmd: str, fields: dict[str, str]) -> LtmsResult:
        cfg = self._config
        last_fault: Optional[Exception] = None
        for attempt in range(cfg.max_attempts):
            try:
                return self._request_once(cmd, fields)
            except LtmsFault as exc:
                last_fault = exc
                if attempt < cfg.max_attempts - 1:
                    time.sleep(cfg.backoff_base * (2 ** attempt))
                    continue
        raise LtmsFault(f"exhausted {cfg.max_attempts} attempts, last error: {last_fault}")

    # ---- public commands --------------------------------------------

    def debug_echo(self, msg: str) -> dict:
        """Round-trip diagnostic. Bypasses fault injection -- do not use this
        to judge health of the operational path (LOAD_QUERY/GET/BOOK)."""
        result = self._request_once("DEBUG_ECHO", {"MSG": msg})
        return result.records[0] if result.records else {}

    def load_query(self, max_results: Optional[int] = None, **filters: str) -> list[dict]:
        if not filters:
            raise ValueError("at least one filter is required (origin/destination/equipment/pickup date)")
        fields = dict(filters)
        if max_results is not None:
            fields["MAX_RESULTS"] = str(max_results)
        result = self._request_with_retry("LOAD_QUERY", fields)
        return result.records

    def load_get(self, load_id: str) -> dict:
        result = self._request_with_retry("LOAD_GET", {"LOAD_ID": load_id})
        return result.records[0]

    def load_book(self, load_id: str, mc_num: str, agreed_rate: int) -> BookResult:
        """Book a load. NOTE ON RETRY SAFETY: if a transport fault occurs
        after the write may have reached the server, a retry can legitimately
        come back ALREADY_BOOKED even though this call never saw a success.
        We surface that as ALREADY_BOOKED_AMBIGUOUS so the caller can log a
        probable-success rather than telling the carrier the booking failed.
        """
        fields = {"LOAD_ID": load_id, "MC_NUM": mc_num, "AGREED_RATE": str(agreed_rate)}
        faulted_first = False
        cfg = self._config
        last_fault: Optional[Exception] = None
        for attempt in range(cfg.max_attempts):
            try:
                result = self._request_once("LOAD_BOOK", fields)
                record = result.records[0]
                return BookResult(
                    outcome=BookOutcome.BOOKED,
                    load_id=record.get("LOAD_ID", load_id),
                    booking_ref=record.get("BOOKING_REF"),
                    timestamp=record.get("TIMESTAMP"),
                )
            except LtmsApplicationError as exc:
                if exc.code == "ALREADY_BOOKED" and faulted_first:
                    return BookResult(outcome=BookOutcome.ALREADY_BOOKED_AMBIGUOUS, load_id=load_id)
                raise
            except LtmsFault as exc:
                last_fault = exc
                faulted_first = True
                if attempt < cfg.max_attempts - 1:
                    time.sleep(cfg.backoff_base * (2 ** attempt))
                    continue
        raise LtmsFault(f"exhausted {cfg.max_attempts} attempts booking {load_id}, last error: {last_fault}")
