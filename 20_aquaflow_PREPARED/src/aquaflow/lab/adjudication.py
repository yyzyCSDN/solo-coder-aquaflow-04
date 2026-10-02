"""Adjudication of duplicate / split laboratory result sets.

Replicate readings for one sample are screened against quality rules
(relative percent difference, blank contamination, holding time).  A set
that fails a rule is held in review until a qualified reviewer accepts,
replaces, or rejects it.  Every decision, the revision it published, and
the supporting evidence are kept on an append-only ledger, so published
results are never silently rewritten and rejected sets stay auditable.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone

REVIEW = "review"
ACCEPTED = "accepted"
REJECTED = "rejected"

ACCEPT = "accept"
REPLACE = "replace"
REJECT = "reject"
ACTIONS = (ACCEPT, REPLACE, REJECT)

READING_KINDS = ("primary", "duplicate", "split")


@dataclass(frozen=True)
class Reading:
    """One replicate measurement of a sample analyte."""
    sample: str
    analyte: str
    value: float
    analyst: str
    measured_at: datetime
    kind: str = "duplicate"


@dataclass(frozen=True)
class Limits:
    """Quality-rule envelope for a duplicate / split set."""
    max_rpd_percent: float
    blank_limit: float
    max_holding_hours: float


@dataclass(frozen=True)
class QcCheck:
    """Outcome of one quality rule, kept as adjudication evidence."""
    name: str
    passed: bool
    value: float
    limit: float


@dataclass(frozen=True)
class Decision:
    """Persisted adjudication outcome for one result set."""
    action: str
    reviewer: str
    reason: str
    at: datetime
    revision: int | None
    evidence: tuple[QcCheck, ...]


@dataclass(frozen=True)
class ResultSet:
    """A group of replicate readings plus its adjudication trail."""
    set_id: str
    sample: str
    analyte: str
    readings: tuple[Reading, ...]
    checks: tuple[QcCheck, ...]
    state: str
    value: float
    revision: int | None
    supersedes: int | None
    submitted_at: datetime
    decision: Decision | None = None


@dataclass(frozen=True)
class Published:
    """One published revision; superseded rows are never removed."""
    sample: str
    analyte: str
    value: float
    revision: int
    supersedes: int | None
    set_id: str
    published_at: datetime
    published_by: str


@dataclass(frozen=True)
class Ledger:
    """Append-only record of result sets and published revisions."""
    sets: tuple[ResultSet, ...] = ()
    published: tuple[Published, ...] = ()


def _utc(at: datetime) -> datetime:
    if at.tzinfo is None:
        return at.replace(tzinfo=timezone.utc)
    return at.astimezone(timezone.utc)


def rpd(a: float, b: float) -> float:
    """Relative percent difference between two replicate readings."""
    if a < 0 or b < 0:
        raise ValueError("negative reading")
    mean = (a + b) / 2.0
    if mean == 0:
        return 0.0
    return abs(a - b) / mean * 100.0


def _worst_rpd(values: list[float]) -> float:
    worst = 0.0
    for i in range(len(values)):
        for j in range(i + 1, len(values)):
            worst = max(worst, rpd(values[i], values[j]))
    return worst


def evaluate(readings: tuple[Reading, ...], blank_value: float, collected_at: datetime,
             limits: Limits) -> tuple[QcCheck, ...]:
    """Run the quality rules for one replicate set."""
    worst = _worst_rpd([r.value for r in readings])
    holding = max((_utc(r.measured_at) - _utc(collected_at)).total_seconds() / 3600.0
                  for r in readings)
    return (
        QcCheck("rpd", worst <= limits.max_rpd_percent, worst, limits.max_rpd_percent),
        QcCheck("blank", blank_value <= limits.blank_limit, blank_value, limits.blank_limit),
        QcCheck("holding_time", holding <= limits.max_holding_hours, holding, limits.max_holding_hours),
    )


def _validate_limits(limits: Limits) -> None:
    if limits.max_rpd_percent < 0:
        raise ValueError("negative rpd limit")
    if limits.blank_limit < 0:
        raise ValueError("negative blank limit")
    if limits.max_holding_hours <= 0:
        raise ValueError("holding limit must be positive")


def _validate_set(readings: tuple[Reading, ...], blank_value: float,
                  collected_at: datetime, at: datetime) -> None:
    if len(readings) < 2:
        raise ValueError("replicate readings required")
    if len({r.sample for r in readings}) != 1 or len({r.analyte for r in readings}) != 1:
        raise ValueError("mixed sample or analyte")
    if any(r.kind not in READING_KINDS for r in readings):
        raise ValueError("unknown reading kind")
    if any(r.value < 0 for r in readings):
        raise ValueError("negative reading")
    if blank_value < 0:
        raise ValueError("negative blank")
    stamps = [_utc(r.measured_at) for r in readings]
    if len(set(stamps)) != len(stamps):
        raise ValueError("indistinguishable reading timestamps")
    if any(t < _utc(collected_at) for t in stamps):
        raise ValueError("reading before collection")
    if any(t > at for t in stamps):
        raise ValueError("reading after submission")


def _active(ledger: Ledger, sample: str, analyte: str) -> Published | None:
    prior = [p for p in ledger.published if p.sample == sample and p.analyte == analyte]
    superseded = {p.supersedes for p in prior if p.supersedes is not None}
    active = [p for p in prior if p.revision not in superseded]
    if len(active) > 1:
        raise ValueError("ambiguous published history")
    return active[0] if active else None


def _publish(ledger: Ledger, sample: str, analyte: str, value: float,
             set_id: str, at: datetime, by: str) -> Published:
    previous = _active(ledger, sample, analyte)
    revision = previous.revision + 1 if previous else 1
    return Published(sample, analyte, value, revision,
                     previous.revision if previous else None,
                     set_id, at, by)


def submit(ledger: Ledger, set_id: str, readings: list[Reading], blank_value: float,
           collected_at: datetime, limits: Limits, at: datetime) -> Ledger:
    """Register a replicate set; failing sets are held for review.

    A set that passes every quality rule is accepted and published as a
    new revision that explicitly supersedes the previous one, so the
    published history is never silently rewritten.
    """
    if any(s.set_id == set_id for s in ledger.sets):
        raise ValueError("duplicate set id")
    _validate_limits(limits)
    rows = tuple(readings)
    at = _utc(at)
    _validate_set(rows, blank_value, collected_at, at)
    checks = evaluate(rows, blank_value, collected_at, limits)
    sample = rows[0].sample
    analyte = rows[0].analyte
    value = sum(r.value for r in rows) / len(rows)
    if any(not c.passed for c in checks):
        held = ResultSet(set_id, sample, analyte, rows, checks, REVIEW,
                         value, None, None, at)
        return replace(ledger, sets=ledger.sets + (held,))
    published = _publish(ledger, sample, analyte, value, set_id, at, "system")
    decision = Decision(ACCEPT, "system", "qc passed", at, published.revision, checks)
    accepted = ResultSet(set_id, sample, analyte, rows, checks, ACCEPTED,
                         value, published.revision, published.supersedes, at, decision)
    return replace(ledger, sets=ledger.sets + (accepted,),
                   published=ledger.published + (published,))


def _swap(sets: tuple[ResultSet, ...], updated: ResultSet) -> tuple[ResultSet, ...]:
    return tuple(updated if s.set_id == updated.set_id else s for s in sets)


def adjudicate(ledger: Ledger, set_id: str, action: str, reviewer: str,
               qualified: frozenset[str] | set[str], at: datetime,
               reason: str = "", value: float | None = None) -> Ledger:
    """Record a reviewer decision for a set held in review.

    Approving a set that failed quality rules requires a documented
    reason; rejection always requires one.  Accept and replace publish a
    new revision linked to the one it supersedes; reject leaves the
    published result untouched and the set auditable.
    """
    if action not in ACTIONS:
        raise ValueError("unknown action")
    target = next((s for s in ledger.sets if s.set_id == set_id), None)
    if target is None:
        raise KeyError("unknown set")
    if target.state != REVIEW:
        raise ValueError("set not in review")
    if reviewer not in qualified:
        raise ValueError("unqualified reviewer")
    if any(reviewer == r.analyst for r in target.readings):
        raise ValueError("reviewer analyzed set")
    at = _utc(at)
    if at < target.submitted_at:
        raise ValueError("time reversal")
    if not reason.strip():
        raise ValueError("reason required")
    if action == REPLACE:
        if value is None:
            raise ValueError("replacement value required")
        if value < 0:
            raise ValueError("negative replacement")
    if action == REJECT:
        decision = Decision(REJECT, reviewer, reason, at, None, target.checks)
        rejected = replace(target, state=REJECTED, decision=decision)
        return replace(ledger, sets=_swap(ledger.sets, rejected))
    published_value = target.value if action == ACCEPT else float(value)
    published = _publish(ledger, target.sample, target.analyte, published_value,
                         set_id, at, reviewer)
    decision = Decision(action, reviewer, reason, at, published.revision, target.checks)
    accepted = replace(target, state=ACCEPTED, revision=published.revision,
                       supersedes=published.supersedes, decision=decision)
    return replace(ledger, sets=_swap(ledger.sets, accepted),
                   published=ledger.published + (published,))


def current(ledger: Ledger, sample: str, analyte: str) -> Published | None:
    """Active published revision for a sample analyte, if any."""
    return _active(ledger, sample, analyte)


def history(ledger: Ledger, sample: str, analyte: str) -> tuple[Published, ...]:
    """Every published revision for a sample analyte, oldest first."""
    return tuple(sorted((p for p in ledger.published
                         if p.sample == sample and p.analyte == analyte),
                        key=lambda p: p.revision))


def get(ledger: Ledger, set_id: str) -> ResultSet:
    for s in ledger.sets:
        if s.set_id == set_id:
            return s
    raise KeyError("unknown set")


def in_review(ledger: Ledger) -> tuple[ResultSet, ...]:
    return tuple(s for s in ledger.sets if s.state == REVIEW)


def rejected(ledger: Ledger) -> tuple[ResultSet, ...]:
    """Rejected sets stay on the ledger and remain auditable."""
    return tuple(s for s in ledger.sets if s.state == REJECTED)
