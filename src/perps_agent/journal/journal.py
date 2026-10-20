"""The decision journal (overview §3.11): append-only, one thread of entries per decision.

The agent bridge (WP10) records each decision before acting on it, then the
risk verdict, then the order execution sent (:meth:`Journal.link_order`), whose
fills the ledger books. :meth:`Journal.record_outcomes` turns each closed
position into a :class:`TradeOutcome` for the decision that opened it, and the
daily review adds a :class:`Review`. :meth:`Journal.assumption_checks` is what
the learning loop (WP14) counts before it changes how a playbook is applied.

Every entry is checked against what is already recorded, so a thread can only
reference decisions, scenarios, assumptions and orders the journal knows.
"""

import hashlib
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from perps_agent.clock import utc_now
from perps_agent.config.models import Mode
from perps_agent.journal.entries import (
    Action,
    ActionFailed,
    AssumptionCheck,
    DecisionMade,
    JournalEntry,
    OrderLinked,
    Review,
    RiskVerdictRecorded,
    SessionStarted,
    SnapshotRef,
    TradeOutcome,
)
from perps_agent.ledger import FillBooked, Ledger, LedgerPosition, OrderPurpose, OrderSent
from perps_agent.market import MarketSnapshot
from perps_agent.risk import RiskVerdict


class JournalError(Exception):
    """An entry that contradicts the journal (unknown decision, duplicate, mismatch)."""


@dataclass(slots=True)
class DecisionTrail:
    """A decision and every later entry about it, in recorded order."""

    decision: DecisionMade
    verdicts: list[RiskVerdictRecorded] = field(default_factory=list)
    orders: list[OrderLinked] = field(default_factory=list)
    failures: list[ActionFailed] = field(default_factory=list)
    outcomes: list[TradeOutcome] = field(default_factory=list)
    reviews: list[Review] = field(default_factory=list)

    @property
    def decision_id(self) -> str:
        return self.decision.decision_id

    @property
    def verdict(self) -> RiskVerdict | None:
        return self.verdicts[-1].verdict if self.verdicts else None

    @property
    def review(self) -> Review | None:
        """The latest review; a later one supersedes an earlier one."""
        return self.reviews[-1] if self.reviews else None

    @property
    def position_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(o.position_id for o in self.orders))


@dataclass(frozen=True, slots=True)
class AssumptionOutcome:
    """One reviewed occurrence of an assumption, for the learning loop's evidence count."""

    decision: DecisionMade
    check: AssumptionCheck
    reviewed_at: datetime


def snapshot_ref(
    snapshot: MarketSnapshot, features: Mapping[str, Decimal] | None = None
) -> SnapshotRef:
    """The compact journal form of a snapshot; ``digest`` is the SHA-256 of its full JSON."""
    digest = hashlib.sha256(snapshot.model_dump_json().encode()).hexdigest()
    return SnapshotRef(
        symbol=snapshot.symbol,
        taken_at=snapshot.taken_at,
        digest=digest,
        mark_price=snapshot.mark_price,
        mid=snapshot.mid,
        spread_bp=snapshot.spread_bp,
        stale=snapshot.stale,
        features=dict(features or {}),
    )


class Journal:
    """The session's decision journal. ``sink`` receives each entry as it is recorded."""

    def __init__(
        self,
        *,
        sink: Callable[[JournalEntry], None] | None = None,
        clock: Callable[[], datetime] = utc_now,
        new_id: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self._sink = sink
        self._clock = clock
        self._new_id = new_id
        self._entries: list[JournalEntry] = []
        self._trails: dict[str, DecisionTrail] = {}
        self._orders: dict[str, OrderLinked] = {}
        self._outcomes: dict[str, TradeOutcome] = {}  # position id → outcome

    @classmethod
    def from_entries(cls, entries: Iterable[JournalEntry]) -> "Journal":
        """Rebuild a journal from saved entries: one session's, or several in date order."""
        journal = cls()
        for entry in entries:
            journal._apply(entry)
            journal._entries.append(entry)
        return journal

    # --- Recording ---

    def record(self, entry: JournalEntry) -> JournalEntry:
        """Check the entry, assign the next sequence number and pass it to the sink."""
        entry = entry.model_copy(update={"sequence": len(self._entries) + 1})
        self._apply(entry)
        self._entries.append(entry)
        if self._sink is not None:
            self._sink(entry)
        return entry

    def start_session(self, day: date, mode: Mode, symbols: Iterable[str]) -> SessionStarted:
        entry = SessionStarted(at=self._clock(), day=day, mode=mode, symbols=tuple(symbols))
        recorded = self.record(entry)
        assert isinstance(recorded, SessionStarted)
        return recorded

    def decide(self, **fields: Any) -> DecisionMade:
        """Record a decision (fields as :class:`DecisionMade`); its id is assigned here."""
        entry = DecisionMade(at=self._clock(), decision_id=self._new_id(), **fields)
        recorded = self.record(entry)
        assert isinstance(recorded, DecisionMade)
        return recorded

    def record_verdict(self, decision_id: str, verdict: RiskVerdict) -> None:
        self.record(RiskVerdictRecorded(at=self._clock(), decision_id=decision_id, verdict=verdict))

    def link_order(self, decision_id: str, client_order_id: str, ledger: Ledger) -> OrderLinked:
        """Tie the order execution sent for a decision to the decision, via the ledger."""
        order = ledger.order(client_order_id)
        if order is None:
            raise JournalError(f"order {client_order_id} is not in the ledger")
        entry = OrderLinked(
            at=self._clock(),
            decision_id=decision_id,
            client_order_id=client_order_id,
            position_id=order.sent.position_id,
        )
        recorded = self.record(entry)
        assert isinstance(recorded, OrderLinked)
        return recorded

    def record_failure(self, decision_id: str, error: str) -> None:
        self.record(ActionFailed(at=self._clock(), decision_id=decision_id, error=error))

    def record_outcomes(self, ledger: Ledger) -> list[TradeOutcome]:
        """Record the outcome of every closed position a journaled entry opened, once."""
        recorded = []
        for link in self._orders.values():
            order = ledger.order(link.client_order_id)
            if order is None or order.sent.purpose is not OrderPurpose.ENTRY:
                continue
            position = ledger.position(link.position_id)
            if position is None or position.is_open or link.position_id in self._outcomes:
                continue
            outcome = self.record(_outcome(self._clock(), link.decision_id, position, ledger))
            assert isinstance(outcome, TradeOutcome)
            recorded.append(outcome)
        return recorded

    def review(self, decision_id: str, **fields: Any) -> Review:
        """Record the agent's review of a decision (fields as :class:`Review`)."""
        entry = Review(at=self._clock(), decision_id=decision_id, **fields)
        recorded = self.record(entry)
        assert isinstance(recorded, Review)
        return recorded

    # --- Queries ---

    @property
    def entries(self) -> tuple[JournalEntry, ...]:
        return tuple(self._entries)

    @property
    def decisions(self) -> tuple[DecisionTrail, ...]:
        return tuple(self._trails.values())

    def decision(self, decision_id: str) -> DecisionTrail | None:
        return self._trails.get(decision_id)

    def unreviewed(self) -> tuple[DecisionTrail, ...]:
        """Decisions still waiting for the daily review (notes are not reviewed)."""
        return tuple(
            t
            for t in self._trails.values()
            if not t.reviews and t.decision.action is not Action.NOTE
        )

    def assumption_checks(
        self,
        key: str,
        *,
        last: int | None = None,
        setup: str | None = None,
        regime: str | None = None,
    ) -> tuple[AssumptionOutcome, ...]:
        """Reviewed occurrences of an assumption, oldest first; ``last`` keeps the newest N.

        Only each decision's latest review counts. ``setup`` and ``regime`` narrow
        the evidence to decisions tagged with them.
        """
        found = []
        for trail in self._trails.values():
            review = trail.review
            if review is None:
                continue
            decision = trail.decision
            if setup is not None and decision.setup != setup:
                continue
            if regime is not None and decision.regime != regime:
                continue
            for check in review.assumptions:
                if check.key == key:
                    found.append(AssumptionOutcome(decision, check, review.at))
        found.sort(key=lambda o: o.reviewed_at)
        return tuple(found[-last:] if last is not None else found)

    # --- Checking entries ---

    def _apply(self, entry: JournalEntry) -> None:
        match entry:
            case SessionStarted():
                pass
            case DecisionMade():
                if entry.decision_id in self._trails:
                    raise JournalError(f"decision {entry.decision_id} recorded twice")
                self._trails[entry.decision_id] = DecisionTrail(entry)
            case RiskVerdictRecorded():
                trail = self._trail(entry.decision_id)
                if entry.verdict.intent != trail.decision.intent:
                    raise JournalError(
                        f"verdict for decision {entry.decision_id} is for a different intent"
                    )
                trail.verdicts.append(entry)
            case OrderLinked():
                trail = self._trail(entry.decision_id)
                if entry.client_order_id in self._orders:
                    raise JournalError(f"order {entry.client_order_id} linked twice")
                self._orders[entry.client_order_id] = entry
                trail.orders.append(entry)
            case ActionFailed():
                self._trail(entry.decision_id).failures.append(entry)
            case TradeOutcome():
                trail = self._trail(entry.decision_id)
                if entry.position_id in self._outcomes:
                    raise JournalError(f"outcome of position {entry.position_id} recorded twice")
                if entry.position_id not in trail.position_ids:
                    raise JournalError(
                        f"position {entry.position_id} is not linked to decision "
                        f"{entry.decision_id}"
                    )
                self._outcomes[entry.position_id] = entry
                trail.outcomes.append(entry)
            case Review():
                trail = self._trail(entry.decision_id)
                decision = trail.decision
                names = {s.name for s in decision.scenarios}
                if entry.realized_scenario is not None and entry.realized_scenario not in names:
                    raise JournalError(
                        f"scenario {entry.realized_scenario} was not weighed in decision "
                        f"{entry.decision_id}"
                    )
                keys = {a.key for a in decision.assumptions}
                unknown = sorted({a.key for a in entry.assumptions} - keys)
                if unknown:
                    raise JournalError(
                        f"assumptions {', '.join(unknown)} are not in decision {entry.decision_id}"
                    )
                trail.reviews.append(entry)

    def _trail(self, decision_id: str) -> DecisionTrail:
        try:
            return self._trails[decision_id]
        except KeyError:
            raise JournalError(f"unknown decision {decision_id}") from None


def _outcome(
    at: datetime, decision_id: str, position: LedgerPosition, ledger: Ledger
) -> TradeOutcome:
    assert position.closed_at is not None
    orders: dict[str, OrderSent] = {}
    exit_value = exit_quantity = Decimal(0)
    last_exit: str | None = None
    for event in ledger.events:
        if isinstance(event, OrderSent) and event.position_id == position.position_id:
            orders[event.client_order_id] = event
        elif isinstance(event, FillBooked) and event.position_id == position.position_id:
            if event.side is not position.direction.side:
                exit_value += event.price * event.quantity
                exit_quantity += event.quantity
                last_exit = event.client_order_id
    reason = None
    if last_exit is not None and last_exit in orders:
        sent = orders[last_exit]
        reason = sent.reason or sent.purpose.value
    risk = position.approved_risk
    return TradeOutcome(
        at=at,
        decision_id=decision_id,
        position_id=position.position_id,
        symbol=position.symbol,
        direction=position.direction,
        level=position.level,
        playbook=position.playbook,
        opened_at=position.opened_at,
        closed_at=position.closed_at,
        max_quantity=position.max_quantity,
        average_entry=position.average_entry,
        average_exit=exit_value / exit_quantity,
        realized_pnl=position.realized_pnl,
        fees=position.fees,
        funding=position.funding,
        net_pnl=position.net_pnl,
        approved_risk=risk,
        r_multiple=position.net_pnl / risk if risk > 0 else None,
        exit_reason=reason,
    )
