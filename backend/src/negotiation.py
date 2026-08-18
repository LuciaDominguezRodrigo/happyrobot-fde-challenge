"""Rate negotiation with a hard, never-disclosed ceiling (max_rate).

Policy (per the Deployment Strategy notes):
- The load is first pitched at loadboard_rate.
- The carrier may accept, reject, or counter.
- Up to three counter-rounds. Each round steps the broker's offer toward
  max_rate (never past it) so we don't concede the whole gap on round one.
- If the carrier's ask is ever <= max_rate, accept it -- no reason to keep
  negotiating once we're inside budget.
- If three rounds pass without the carrier coming down to max_rate or below,
  the negotiation fails: close professionally, log as failed, do not transfer.
- max_rate itself is never returned to a caller-facing field; only
  counter_rate (a computed number, always <= max_rate) is exposed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

MAX_ROUNDS = 3


class NegotiationAction(Enum):
    ACCEPT = "accept"
    COUNTER = "counter"
    REJECT = "reject"


@dataclass
class NegotiationDecision:
    action: NegotiationAction
    rate: int  # agreed rate (ACCEPT) or the counter-offer to present (COUNTER); unset (0) on REJECT
    round_number: int
    rounds_remaining: int


@dataclass
class NegotiationSession:
    load_id: str
    loadboard_rate: int
    max_rate: int
    _round: int = field(default=0, repr=False)

    def evaluate(self, carrier_offer: int) -> NegotiationDecision:
        if carrier_offer <= self.max_rate:
            return NegotiationDecision(
                action=NegotiationAction.ACCEPT,
                rate=carrier_offer,
                round_number=self._round,
                rounds_remaining=MAX_ROUNDS - self._round,
            )

        if self._round >= MAX_ROUNDS:
            return NegotiationDecision(
                action=NegotiationAction.REJECT,
                rate=0,
                round_number=self._round,
                rounds_remaining=0,
            )

        self._round += 1
        step_target = self.loadboard_rate + (self.max_rate - self.loadboard_rate) * self._round // MAX_ROUNDS
        counter_rate = min(step_target, carrier_offer - 1, self.max_rate)
        counter_rate = max(counter_rate, self.loadboard_rate)
        return NegotiationDecision(
            action=NegotiationAction.COUNTER,
            rate=counter_rate,
            round_number=self._round,
            rounds_remaining=MAX_ROUNDS - self._round,
        )
