import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from negotiation import NegotiationAction, NegotiationSession


def test_accepts_immediately_when_offer_within_ceiling():
    session = NegotiationSession(load_id="L1", loadboard_rate=2000, max_rate=2500)
    decision = session.evaluate(carrier_offer=2100)
    assert decision.action == NegotiationAction.ACCEPT
    assert decision.rate == 2100
    assert decision.round_number == 0


def test_accepts_offer_exactly_at_ceiling():
    session = NegotiationSession(load_id="L1", loadboard_rate=2000, max_rate=2500)
    decision = session.evaluate(carrier_offer=2500)
    assert decision.action == NegotiationAction.ACCEPT
    assert decision.rate == 2500


def test_counters_when_offer_exceeds_ceiling_and_never_exceeds_ceiling():
    session = NegotiationSession(load_id="L1", loadboard_rate=2000, max_rate=2500)
    decision = session.evaluate(carrier_offer=4000)
    assert decision.action == NegotiationAction.COUNTER
    assert decision.rate <= 2500
    assert decision.rate >= 2000
    assert decision.round_number == 1
    assert decision.rounds_remaining == 2


def test_final_round_counter_equals_ceiling_and_is_never_exceeded():
    session = NegotiationSession(load_id="L1", loadboard_rate=2000, max_rate=2500)
    session.evaluate(carrier_offer=9000)  # round 1
    session.evaluate(carrier_offer=9000)  # round 2
    decision = session.evaluate(carrier_offer=9000)  # round 3 -- final offer
    assert decision.action == NegotiationAction.COUNTER
    assert decision.rate == 2500
    assert decision.rounds_remaining == 0


def test_rejects_after_three_rounds_if_carrier_never_comes_down():
    session = NegotiationSession(load_id="L1", loadboard_rate=2000, max_rate=2500)
    session.evaluate(carrier_offer=9000)
    session.evaluate(carrier_offer=9000)
    session.evaluate(carrier_offer=9000)
    decision = session.evaluate(carrier_offer=9000)
    assert decision.action == NegotiationAction.REJECT
    assert decision.rate == 0


def test_carrier_accepting_a_counter_ends_negotiation_favorably():
    session = NegotiationSession(load_id="L1", loadboard_rate=2000, max_rate=2500)
    first = session.evaluate(carrier_offer=9000)
    decision = session.evaluate(carrier_offer=first.rate)
    assert decision.action == NegotiationAction.ACCEPT
    assert decision.rate == first.rate
