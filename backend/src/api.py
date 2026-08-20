"""HTTP layer the HappyRobot workflow calls into during a carrier call.

Design constraints driving the shape of this API:
- max_rate never appears in any response body. Negotiation is evaluated
  server-side (POST /negotiate) so the ceiling never enters the LLM's
  context and can't be disclosed, directly or indirectly, by the agent.
- OTP codes never appear in any response body either -- /otp/send only
  confirms dispatch; the code exists solely inside the notification payload.
- All endpoints require a bearer token (the workflow's own credential, not
  the carrier's) since this service is reachable from the public internet
  once deployed.
- Negotiation and call state live in-process (in-memory), not in an
  external database -- structured call outcomes (MC, load id, agreed rate,
  result) are written to HappyRobot Twin tables directly from the workflow,
  per the requirement that Twin be the data layer of record. This service
  holds only ephemeral per-call negotiation counters.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Header
from fastapi.responses import FileResponse
from pydantic import BaseModel

from call_log import CallLogStore
from fmcsa_client import AuthorityStatus, FmcsaClient
from negotiation import NegotiationAction, NegotiationSession
from otp import LoggingNotificationSender, OtpService
from tms_client import LtmsApplicationError, LtmsClient, LtmsClientConfig, LtmsFault, BookOutcome

app = FastAPI(title="HappyRobot Carrier Desk Integration API")

API_AUTH_TOKEN = os.environ["API_AUTH_TOKEN"]

_tms = LtmsClient(LtmsClientConfig(
    host=os.environ["TMS_HOST"],
    port=int(os.environ["TMS_PORT"]),
    token=os.environ["TMS_TOKEN"],
))
_fmcsa = FmcsaClient(web_key=os.environ["FMCSA_WEB_KEY"])
_otp = OtpService(sender=LoggingNotificationSender())
_negotiations: dict[tuple[str, str], NegotiationSession] = {}
_call_log = CallLogStore()


def require_auth(authorization: str = Header(default="")) -> None:
    if authorization != f"Bearer {API_AUTH_TOKEN}":
        raise HTTPException(status_code=401, detail="unauthorized")


def _public_load_fields(record: dict) -> dict:
    """Strip everything not meant to leave this service -- notably MAX_BUY."""
    return {
        "load_id": record.get("LOAD_ID"),
        "origin": f"{record.get('ORIG_CITY', '').strip()}, {record.get('ORIG_STATE', '')}",
        "destination": f"{record.get('DEST_CITY', '').strip()}, {record.get('DEST_STATE', '')}",
        "pickup_datetime": record.get("PICKUP_DT"),
        "delivery_datetime": record.get("DELIVERY_DT"),
        "equipment_type": record.get("EQTYPE", "").strip(),
        "loadboard_rate": _as_int(record.get("RATE")),
        "weight": _as_int(record.get("WEIGHT")),
        "commodity_type": record.get("COMMODITY", "").strip() or None,
        "num_of_pieces": _as_int(record.get("PIECES")),
        "miles": _as_int(record.get("MILES")),
        "dimensions": record.get("DIMS", "").strip() or None,
        "notes": record.get("NOTES", "").strip() or None,
    }


def _as_int(value: Optional[str]) -> Optional[int]:
    if value is None or value == "":
        return None
    return int(value)


# ---- carrier verification -------------------------------------------

class VerifyCarrierRequest(BaseModel):
    mc_number: str
    call_id: Optional[str] = None


@app.post("/verify-carrier", dependencies=[Depends(require_auth)])
def verify_carrier(req: VerifyCarrierRequest):
    result = _fmcsa.check_authority(req.mc_number)
    _call_log.upsert(
        req.call_id,
        mc_number=result.mc_number,
        carrier_legal_name=result.legal_name,
        fmcsa_status=result.status.value,
    )
    return {
        "mc_number": result.mc_number,
        "status": result.status.value,
        "legal_name": result.legal_name,
        "dot_number": result.dot_number,
        "raw": result.raw,
    }


# ---- OTP ---------------------------------------------------------------

class SendOtpRequest(BaseModel):
    call_id: str
    destination: str  # email or phone number


@app.post("/otp/send", dependencies=[Depends(require_auth)])
def send_otp(req: SendOtpRequest):
    _otp.send_otp(req.call_id, req.destination)
    _call_log.upsert(req.call_id, otp_destination=req.destination)
    return {"sent": True}


class VerifyOtpRequest(BaseModel):
    call_id: str
    code: str


@app.post("/otp/verify", dependencies=[Depends(require_auth)])
def verify_otp(req: VerifyOtpRequest):
    verified = _otp.verify_otp(req.call_id, req.code)
    _call_log.increment(req.call_id, "otp_attempts")
    if verified:
        _call_log.upsert(req.call_id, otp_verified=1)
    return {"verified": verified}


# ---- load search / detail ----------------------------------------------

class SearchLoadsRequest(BaseModel):
    origin_city: Optional[str] = None
    origin_state: Optional[str] = None
    dest_city: Optional[str] = None
    dest_state: Optional[str] = None
    equipment_type: Optional[str] = None
    max_results: Optional[int] = 5


@app.post("/loads/search", dependencies=[Depends(require_auth)])
def search_loads(req: SearchLoadsRequest):
    filters = {}
    if req.origin_city:
        filters["ORIG_CITY"] = req.origin_city
    if req.origin_state:
        filters["ORIG_STATE"] = req.origin_state
    if req.dest_city:
        filters["DEST_CITY"] = req.dest_city
    if req.dest_state:
        filters["DEST_STATE"] = req.dest_state
    if req.equipment_type:
        filters["EQTYPE"] = req.equipment_type
    if not filters:
        raise HTTPException(status_code=400, detail="at least one of origin/destination/equipment is required")

    try:
        records = _tms.load_query(max_results=req.max_results, **filters)
    except LtmsFault:
        raise HTTPException(status_code=503, detail="load board is temporarily unavailable, please try again shortly")
    return {"loads": [_public_load_fields(r) for r in records]}


@app.get("/loads/{load_id}", dependencies=[Depends(require_auth)])
def get_load(load_id: str):
    try:
        record = _tms.load_get(load_id)
    except LtmsApplicationError as exc:
        raise HTTPException(status_code=404, detail=exc.msg)
    except LtmsFault:
        raise HTTPException(status_code=503, detail="load board is temporarily unavailable, please try again shortly")
    return _public_load_fields(record)


# ---- negotiation ---------------------------------------------------------

class NegotiateRequest(BaseModel):
    call_id: str
    load_id: str
    carrier_offer: int


@app.post("/negotiate", dependencies=[Depends(require_auth)])
def negotiate(req: NegotiateRequest):
    key = (req.call_id, req.load_id)
    session = _negotiations.get(key)
    if session is None:
        try:
            record = _tms.load_get(req.load_id)
        except LtmsApplicationError as exc:
            raise HTTPException(status_code=404, detail=exc.msg)
        except LtmsFault:
            raise HTTPException(status_code=503, detail="load board is temporarily unavailable, please try again shortly")
        max_rate = _as_int(record.get("MAX_BUY"))
        if max_rate is None:
            raise HTTPException(status_code=409, detail="negotiation ceiling unavailable for this load")
        session = NegotiationSession(load_id=req.load_id, loadboard_rate=_as_int(record.get("RATE")), max_rate=max_rate)
        _negotiations[key] = session
        _call_log.upsert(req.call_id, load_id=req.load_id, loadboard_rate=session.loadboard_rate)

    decision = session.evaluate(req.carrier_offer)
    log_fields = {
        "last_offer": req.carrier_offer,
        "last_action": decision.action.value,
        "negotiation_rounds": decision.round_number,
    }
    if decision.action == NegotiationAction.REJECT:
        log_fields["outcome"] = "negotiation_failed"
    _call_log.upsert(req.call_id, **log_fields)
    return {
        "action": decision.action.value,
        "rate": decision.rate if decision.action != NegotiationAction.REJECT else None,
        "round_number": decision.round_number,
        "rounds_remaining": decision.rounds_remaining,
    }


# ---- booking --------------------------------------------------------------

class BookLoadRequest(BaseModel):
    mc_number: str
    agreed_rate: int
    call_id: Optional[str] = None


@app.post("/loads/{load_id}/book", dependencies=[Depends(require_auth)])
def book_load(load_id: str, req: BookLoadRequest):
    try:
        result = _tms.load_book(load_id, mc_num=req.mc_number, agreed_rate=req.agreed_rate)
    except LtmsApplicationError as exc:
        _call_log.upsert(req.call_id, outcome="booking_failed")
        raise HTTPException(status_code=409, detail=exc.msg)
    except LtmsFault:
        raise HTTPException(status_code=503, detail="booking system is temporarily unavailable, please try again shortly")

    _call_log.upsert(
        req.call_id,
        agreed_rate=req.agreed_rate,
        outcome=result.outcome.value,
        booking_ref=result.booking_ref,
    )
    return {
        "outcome": result.outcome.value,
        "load_id": result.load_id,
        "booking_ref": result.booking_ref,
        "timestamp": result.timestamp,
    }


# ---- ops dashboard ---------------------------------------------------------

@app.get("/ops/calls", dependencies=[Depends(require_auth)])
def ops_calls(limit: int = 200):
    return {"calls": _call_log.list_calls(limit=limit)}


@app.get("/ops/summary", dependencies=[Depends(require_auth)])
def ops_summary():
    return _call_log.summary()


class FlagCallRequest(BaseModel):
    flagged: bool = True


@app.post("/ops/calls/{call_id}/flag", dependencies=[Depends(require_auth)])
def flag_call(call_id: str, req: FlagCallRequest):
    """Ops-manager action: flag a call for follow-up (e.g. a borderline
    negotiation or a carrier worth a relationship check-in). Local to the
    ops mirror -- does not touch Twin, which stays the system of record for
    the call outcome itself."""
    _call_log.upsert(call_id, flagged=1 if req.flagged else 0)
    return {"call_id": call_id, "flagged": req.flagged}


_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@app.get("/ops")
def ops_dashboard():
    return FileResponse(_STATIC_DIR / "ops.html")


@app.get("/health")
def health():
    return {"ok": True}
