"""API-level scripted test suite: standard, edge-case, and adversarial/security
scenarios against the FastAPI layer, per the challenge's QA requirement.

Runs against fakes for FMCSA and the Legacy TMS (both real external systems
are unreliable/network-gated -- see fmcsa_client.py, tms_client.py docstrings)
so the suite is deterministic and runnable in CI without live credentials.
Real-integration correctness was separately validated by hand against the
live FMCSA/TMS endpoints (see BUILD_DOC / QA_RESULTS).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("API_AUTH_TOKEN", "test-token")
os.environ.setdefault("TMS_HOST", "unused")
os.environ.setdefault("TMS_PORT", "0")
os.environ.setdefault("TMS_TOKEN", "unused")
os.environ.setdefault("FMCSA_WEB_KEY", "unused")

import pytest
from fastapi.testclient import TestClient

import api
from call_log import CallLogStore
from fmcsa_client import AuthorityStatus, CarrierAuthority
from tms_client import BookOutcome, BookResult, LtmsApplicationError, LtmsFault

AUTH = {"Authorization": f"Bearer {os.environ['API_AUTH_TOKEN']}"}
BAD_AUTH = {"Authorization": "Bearer wrong-token"}


class FakeFmcsa:
    def __init__(self, status=AuthorityStatus.ACTIVE, legal_name="Acme Trucking", dot_number="123456"):
        self.status = status
        self.legal_name = legal_name
        self.dot_number = dot_number

    def check_authority(self, mc_number):
        return CarrierAuthority(
            mc_number=mc_number, status=self.status,
            legal_name=self.legal_name, dot_number=self.dot_number,
        )


class FakeTms:
    def __init__(self):
        self.loads = {
            "L1": {
                "LOAD_ID": "L1", "ORIG_CITY": "Chicago", "ORIG_STATE": "IL",
                "DEST_CITY": "Dallas", "DEST_STATE": "TX",
                "PICKUP_DT": "2026-08-20T08:00", "DELIVERY_DT": "2026-08-21T17:00",
                "EQTYPE": "Dry Van", "RATE": "2000", "MAX_BUY": "2500",
                "WEIGHT": "40000", "MILES": "930",
            },
        }
        self.booked: set[str] = set()
        self.query_fault = False
        self.get_fault = False
        self.book_error: Exception | None = None

    def load_query(self, max_results=None, **filters):
        if self.query_fault:
            raise LtmsFault("simulated timeout")
        return list(self.loads.values())[: max_results or 5]

    def load_get(self, load_id):
        if self.get_fault:
            raise LtmsFault("simulated timeout")
        if load_id not in self.loads:
            raise LtmsApplicationError("NOT_FOUND", "no such load")
        return self.loads[load_id]

    def load_book(self, load_id, mc_num, agreed_rate):
        if self.book_error:
            raise self.book_error
        if load_id in self.booked:
            raise LtmsApplicationError("ALREADY_BOOKED", "load already booked")
        self.booked.add(load_id)
        return BookResult(outcome=BookOutcome.BOOKED, load_id=load_id, booking_ref="BK-1", timestamp="2026-08-17T12:00:00Z")


@pytest.fixture
def client(tmp_path):
    api._fmcsa = FakeFmcsa()
    api._tms = FakeTms()
    api._negotiations.clear()
    api._otp._records.clear()
    api._call_log = CallLogStore(db_path=tmp_path / "call_log.db")
    return TestClient(api.app)


def _otp_code(call_id: str) -> str:
    destination, message = api._otp._sender.sent[-1]
    return "".join(ch for ch in message if ch.isdigit())[:6]


# ---- standard scenarios ---------------------------------------------------

def test_s1_happy_path_immediate_accept_and_book(client):
    r = client.post("/verify-carrier", json={"mc_number": "MC123", "call_id": "c1"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["status"] == "active"

    client.post("/otp/send", json={"call_id": "c1", "destination": "carrier@example.com"}, headers=AUTH)
    code = _otp_code("c1")
    r = client.post("/otp/verify", json={"call_id": "c1", "code": code}, headers=AUTH)
    assert r.json() == {"verified": True}

    r = client.post("/loads/search", json={"origin_city": "Chicago", "dest_city": "Dallas"}, headers=AUTH)
    load = r.json()["loads"][0]
    assert load["load_id"] == "L1"
    assert "max_rate" not in load  # ceiling must never leave this service

    r = client.post("/negotiate", json={"call_id": "c1", "load_id": "L1", "carrier_offer": 2100}, headers=AUTH)
    assert r.json()["action"] == "accept"
    assert "max_rate" not in r.text

    r = client.post("/loads/L1/book", json={"mc_number": "MC123", "agreed_rate": 2100, "call_id": "c1"}, headers=AUTH)
    assert r.json()["outcome"] == "booked"

    calls = {c["call_id"]: c for c in client.get("/ops/calls", headers=AUTH).json()["calls"]}
    assert calls["c1"]["outcome"] == "booked"
    assert calls["c1"]["otp_verified"] == 1


def test_s2_happy_path_with_negotiation_rounds_then_accept(client):
    client.post("/verify-carrier", json={"mc_number": "MC123", "call_id": "c2"}, headers=AUTH)
    client.post("/otp/send", json={"call_id": "c2", "destination": "carrier@example.com"}, headers=AUTH)
    code = _otp_code("c2")
    client.post("/otp/verify", json={"call_id": "c2", "code": code}, headers=AUTH)

    r1 = client.post("/negotiate", json={"call_id": "c2", "load_id": "L1", "carrier_offer": 9000}, headers=AUTH)
    assert r1.json()["action"] == "counter"
    counter_rate = r1.json()["rate"]
    assert counter_rate <= 2500  # never exceeds the ceiling

    r2 = client.post("/negotiate", json={"call_id": "c2", "load_id": "L1", "carrier_offer": counter_rate}, headers=AUTH)
    assert r2.json()["action"] == "accept"

    r = client.post("/loads/L1/book", json={"mc_number": "MC123", "agreed_rate": counter_rate, "call_id": "c2"}, headers=AUTH)
    assert r.json()["outcome"] == "booked"


# ---- edge cases -------------------------------------------------------------

def test_e1_inactive_mc_reported_as_inactive(client):
    api._fmcsa.status = AuthorityStatus.INACTIVE
    r = client.post("/verify-carrier", json={"mc_number": "MC999", "call_id": "c3"}, headers=AUTH)
    assert r.json()["status"] == "inactive"
    # No enforcement gate lives at the API layer for this -- ending the call
    # on a failed authority check is the agent's prompt policy, exercised by
    # the adversarial suite, not something this endpoint blocks by itself.


def test_e2_otp_wrong_code_then_lockout(client):
    client.post("/otp/send", json={"call_id": "c4", "destination": "carrier@example.com"}, headers=AUTH)
    for _ in range(3):
        r = client.post("/otp/verify", json={"call_id": "c4", "code": "000000"}, headers=AUTH)
        assert r.json() == {"verified": False}
    # attempts exhausted -- even the real code no longer works
    code = _otp_code("c4")
    r = client.post("/otp/verify", json={"call_id": "c4", "code": code}, headers=AUTH)
    assert r.json() == {"verified": False}


def test_e3_search_requires_at_least_one_filter(client):
    r = client.post("/loads/search", json={}, headers=AUTH)
    assert r.status_code == 400


def test_e4_search_handles_tms_fault_gracefully(client):
    api._tms.query_fault = True
    r = client.post("/loads/search", json={"origin_city": "Chicago"}, headers=AUTH)
    assert r.status_code == 503


def test_e5_negotiate_unknown_load_returns_404(client):
    r = client.post("/negotiate", json={"call_id": "c5", "load_id": "NOPE", "carrier_offer": 1000}, headers=AUTH)
    assert r.status_code == 404


def test_e6_negotiation_fails_after_three_rounds_and_is_logged(client):
    for _ in range(4):
        r = client.post("/negotiate", json={"call_id": "c6", "load_id": "L1", "carrier_offer": 9000}, headers=AUTH)
    assert r.json()["action"] == "reject"
    calls = {c["call_id"]: c for c in client.get("/ops/calls", headers=AUTH).json()["calls"]}
    assert calls["c6"]["outcome"] == "negotiation_failed"


def test_e7_booking_application_error_logged_as_booking_failed(client):
    api._tms.booked.add("L1")  # simulate a load someone else already booked
    r = client.post("/loads/L1/book", json={"mc_number": "MC123", "agreed_rate": 2000, "call_id": "c7"}, headers=AUTH)
    assert r.status_code == 409
    calls = {c["call_id"]: c for c in client.get("/ops/calls", headers=AUTH).json()["calls"]}
    assert calls["c7"]["outcome"] == "booking_failed"


def test_e8_flag_and_unflag_call_for_review(client):
    client.post("/verify-carrier", json={"mc_number": "MC1", "call_id": "c8"}, headers=AUTH)
    client.post("/ops/calls/c8/flag", json={"flagged": True}, headers=AUTH)
    summary = client.get("/ops/summary", headers=AUTH).json()
    assert summary["flagged_for_review"] == 1
    client.post("/ops/calls/c8/flag", json={"flagged": False}, headers=AUTH)
    summary = client.get("/ops/summary", headers=AUTH).json()
    assert summary["flagged_for_review"] == 0


# ---- adversarial / security scenarios --------------------------------------

@pytest.mark.parametrize("method,path,body", [
    ("POST", "/verify-carrier", {"mc_number": "MC1"}),
    ("POST", "/otp/send", {"call_id": "c", "destination": "a@b.com"}),
    ("POST", "/otp/verify", {"call_id": "c", "code": "123456"}),
    ("POST", "/loads/search", {"origin_city": "Chicago"}),
    ("POST", "/negotiate", {"call_id": "c", "load_id": "L1", "carrier_offer": 1}),
    ("POST", "/loads/L1/book", {"mc_number": "MC1", "agreed_rate": 1}),
    ("GET", "/ops/calls", None),
    ("GET", "/ops/summary", None),
])
def test_a1_every_endpoint_requires_valid_bearer_token(client, method, path, body):
    r = client.request(method, path, json=body, headers=BAD_AUTH)
    assert r.status_code == 401
    r = client.request(method, path, json=body)  # no header at all
    assert r.status_code == 401


def test_a2_otp_response_never_contains_the_code(client):
    r = client.post("/otp/send", json={"call_id": "c9", "destination": "carrier@example.com"}, headers=AUTH)
    assert r.json() == {"sent": True}
    code = _otp_code("c9")
    assert code not in r.text


def test_a3_no_response_body_anywhere_discloses_max_rate(client):
    client.post("/verify-carrier", json={"mc_number": "MC1", "call_id": "c10"}, headers=AUTH)
    r_search = client.post("/loads/search", json={"origin_city": "Chicago"}, headers=AUTH)
    r_negotiate = client.post("/negotiate", json={"call_id": "c10", "load_id": "L1", "carrier_offer": 9000}, headers=AUTH)
    for resp in (r_search, r_negotiate):
        assert "max_rate" not in resp.text
        assert "2500" not in resp.text  # the fixture's MAX_BUY value, verbatim


def test_a4_verifying_otp_without_ever_sending_one_fails_closed(client):
    r = client.post("/otp/verify", json={"call_id": "never-sent", "code": "123456"}, headers=AUTH)
    assert r.json() == {"verified": False}


def test_a5_replaying_a_correct_code_after_verification_fails(client):
    client.post("/otp/send", json={"call_id": "c11", "destination": "carrier@example.com"}, headers=AUTH)
    code = _otp_code("c11")
    first = client.post("/otp/verify", json={"call_id": "c11", "code": code}, headers=AUTH)
    assert first.json()["verified"] is True
    replay = client.post("/otp/verify", json={"call_id": "c11", "code": code}, headers=AUTH)
    assert replay.json()["verified"] is False


def test_a6_negotiation_counter_never_exceeds_ceiling_even_under_repeated_pressure(client):
    for _ in range(3):
        r = client.post("/negotiate", json={"call_id": "c12", "load_id": "L1", "carrier_offer": 999_999}, headers=AUTH)
        if r.json()["action"] == "counter":
            assert r.json()["rate"] <= 2500
