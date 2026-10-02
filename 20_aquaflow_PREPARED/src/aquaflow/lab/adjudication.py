"""Adjudication workflow for laboratory duplicate and split-sample results.

A duplicate (or split) group pairs the original reading with one or more
retest readings of the same sample and analyte.  Registration computes the
relative percent difference (RPD) for every original/retest pair and checks
the associated method blank and sample holding time.  Any failed QC rule
places the sample in the ``review`` state; a qualified reviewer may then
accept, replace, or reject the whole group.

Decisions, the revised result that becomes current, and the evidence used
are recorded by :class:`AdjudicationLedger`.  Already-published results are
never silently rewritten: accepting or replacing against a published result
requires explicit acknowledgement and creates a new revision, and rejected
groups remain stored and auditable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

ORIGINAL_ID = "original"

# Submission/QC states
STATE_QC_PASSED = "qc_passed"
STATE_REVIEW = "review"
STATE_ACCEPTED = "accepted"
STATE_REPLACED = "replaced"
STATE_REJECTED = "rejected"

# Terminal states that still expose the group for audit
TERMINAL_STATES = frozenset({STATE_ACCEPTED, STATE_REPLACED, STATE_REJECTED})
# States whose chosen value still has to reach publication
PENDING_REPUBLISH_STATES = frozenset({STATE_ACCEPTED, STATE_REPLACED})

# Stable, machine-readable QC failure codes
FAIL_RPD = "rpd"
FAIL_BLANK = "blank"
FAIL_HOLDING = "holding_time"


def _now() -> datetime:
    return datetime.now().astimezone()


@dataclass(frozen=True)
class Reading:
    """One instrument reading.  ``kind`` is ``original`` or ``retest``."""

    value: float
    measured_at: datetime
    kind: str = "retest"
    reading_id: str = ""


@dataclass(frozen=True)
class BlankEvidence:
    blank_value: float
    blank_limit: float
    passed: bool


@dataclass(frozen=True)
class HoldingEvidence:
    elapsed_hours: float
    holding_time_hours: float
    passed: bool


@dataclass(frozen=True)
class RpdPair:
    reading_id: str
    original_value: float
    retest_value: float
    rpd_percent: float


@dataclass(frozen=True)
class DuplicateGroup:
    group_id: str
    sample: str
    analyte: str
    original: Reading
    retests: tuple[Reading, ...]
    collected_at: datetime
    blank: BlankEvidence
    holding: HoldingEvidence
    rpd_limit_percent: float
    rpd_pairs: tuple[RpdPair, ...]
    failed_checks: tuple[str, ...]
    state: str
    submitted_at: datetime

    @property
    def qc_passed(self) -> bool:
        return not self.failed_checks

    @property
    def readings(self) -> tuple[Reading, ...]:
        return (self.original,) + self.retests

    def requires_review(self) -> bool:
        return self.state == STATE_REVIEW


def _check_reading(value: object, measured_at: object, reading_id: object = "") -> Reading:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError("reading value must be numeric")
    fvalue = float(value)
    if fvalue != fvalue or fvalue in (float("inf"), float("-inf")):
        raise ValueError("reading value must be finite")
    if not isinstance(measured_at, datetime):
        raise TypeError("measured_at must be a datetime")
    return Reading(fvalue, measured_at, "retest", str(reading_id))


def rpd_percent(a: float, b: float) -> float:
    """Relative percent difference of two readings.

    ``|a - b| / mean(a, b) * 100``.  Two non-negative readings at the
    detection limit (both zero) agree by definition, so the RPD is 0.
    """
    if a < 0 or b < 0:
        raise ValueError("readings must be non-negative")
    mean = (a + b) / 2.0
    if mean == 0.0:
        return 0.0
    return abs(a - b) / mean * 100.0


def register_group(
    group_id: str,
    sample: str,
    analyte: str,
    original: tuple[float, datetime] | Reading,
    retests: list[tuple[float, datetime] | Reading],
    collected_at: datetime,
    blank_value: float,
    blank_limit: float,
    holding_time_hours: float,
    analyzed_at: datetime | None = None,
    rpd_limit_percent: float = 20.0,
    submitted_at: datetime | None = None,
) -> DuplicateGroup:
    """Validate a duplicate/split submission and evaluate its QC rules.

    Raises ``ValueError`` when:
      * no retest reading is supplied,
      * retest reading ids are duplicated or collide with ``original``,
      * two readings share the same measurement timestamp,
      * a retest is measured before the original reading,
      * the blank or holding-time envelope is invalid.
    """
    if not str(group_id):
        raise ValueError("group_id is required")
    if not str(sample) or not str(analyte):
        raise ValueError("sample and analyte are required")
    if not isinstance(collected_at, datetime):
        raise TypeError("collected_at must be a datetime")
    if blank_limit < 0:
        raise ValueError("blank limit must be non-negative")
    if blank_value < 0:
        raise ValueError("blank value must be non-negative")
    if holding_time_hours <= 0:
        raise ValueError("holding time must be positive")
    if rpd_limit_percent <= 0:
        raise ValueError("RPD limit must be positive")

    if isinstance(original, Reading):
        orig = Reading(float(original.value), original.measured_at, "original", ORIGINAL_ID)
    else:
        orig = Reading(float(original[0]), original[1], "original", ORIGINAL_ID)
    if orig.value < 0:
        raise ValueError("readings must be non-negative")

    if not retests:
        raise ValueError("duplicate group requires at least one retest reading")

    rows: list[Reading] = []
    seen_ids: set[str] = set()
    seen_times: dict[datetime, str] = {orig.measured_at: ORIGINAL_ID}
    for idx, item in enumerate(retests):
        if isinstance(item, Reading):
            rid = item.reading_id or f"retest_{idx + 1}"
            reading = _check_reading(item.value, item.measured_at, rid)
        else:
            rid = f"retest_{idx + 1}"
            reading = _check_reading(item[0], item[1], rid)
        if rid == ORIGINAL_ID:
            raise ValueError(f"reading id {ORIGINAL_ID!r} is reserved for the original result")
        if rid in seen_ids:
            raise ValueError(f"duplicate retest reading id: {rid}")
        if reading.measured_at in seen_times:
            raise ValueError(
                f"identical measurement timestamp for readings "
                f"{seen_times[reading.measured_at]!r} and {rid!r}"
            )
        if reading.measured_at < orig.measured_at:
            raise ValueError(f"retest {rid!r} was measured before the original reading")
        seen_ids.add(rid)
        seen_times[reading.measured_at] = rid
        rows.append(reading)

    pairs = tuple(
        RpdPair(r.reading_id, orig.value, r.value, rpd_percent(orig.value, r.value))
        for r in rows
    )
    # RPD is reported once even if several pairs exceed the limit.
    failed: list[str] = []
    if any(pair.rpd_percent > rpd_limit_percent for pair in pairs):
        failed.append(FAIL_RPD)

    blank = BlankEvidence(blank_value, blank_limit, blank_value <= blank_limit)
    if not blank.passed:
        failed.append(FAIL_BLANK)

    anchor = analyzed_at or max(r.measured_at for r in rows)
    elapsed_hours = (anchor - collected_at).total_seconds() / 3600.0
    if elapsed_hours < 0:
        raise ValueError("analysis timestamp precedes collection timestamp")
    holding = HoldingEvidence(elapsed_hours, holding_time_hours, elapsed_hours <= holding_time_hours)
    if not holding.passed:
        failed.append(FAIL_HOLDING)

    return DuplicateGroup(
        group_id=str(group_id),
        sample=str(sample),
        analyte=str(analyte),
        original=orig,
        retests=tuple(rows),
        collected_at=collected_at,
        blank=blank,
        holding=holding,
        rpd_limit_percent=rpd_limit_percent,
        rpd_pairs=pairs,
        failed_checks=tuple(failed),
        state=STATE_REVIEW if failed else STATE_QC_PASSED,
        submitted_at=submitted_at or _now(),
    )


@dataclass(frozen=True)
class Reviewer:
    name: str
    role: str
    qualified_analytes: frozenset[str] = frozenset()

    def qualifies_for(self, analyte: str) -> bool:
        # An empty qualification set reads as a general QC reviewer.
        return not self.qualified_analytes or analyte in self.qualified_analytes


@dataclass(frozen=True)
class SelectedVersion:
    """The revised result chosen by an adjudication."""

    sample: str
    analyte: str
    value: float
    source: str  # "duplicate_mean" | "reading"
    source_reading_id: str | None
    revision: int
    supersedes: int | None
    republish_required: bool


@dataclass(frozen=True)
class Adjudication:
    group_id: str
    sample: str
    analyte: str
    decision: str  # accept | replace | reject
    reviewer: str
    reviewer_role: str
    reason: str
    selected: SelectedVersion | None
    failed_checks: tuple[str, ...]
    qc_passed: bool
    published_at_submission: bool
    evidence: dict[str, object]
    decided_at: datetime


@dataclass(frozen=True)
class AuditEvent:
    seq: int
    event: str
    group_id: str
    at: datetime
    detail: dict[str, object] = field(default_factory=dict)


def adjudicate(
    group: DuplicateGroup,
    reviewer: Reviewer,
    decision: str,
    reason: str,
    *,
    replacement_reading_id: str | None = None,
    acknowledge_published: bool = False,
    published: bool = False,
    next_revision: int = 1,
    current_revision: int | None = None,
    decided_at: datetime | None = None,
) -> Adjudication:
    """Pure decision step shared by callers and the ledger.

    Rules:
      * the reviewer must be qualified for the analyte;
      * a group already decided cannot be adjudicated again;
      * accepting or replacing a value requires a non-empty reason;
      * approving failed QC (accept/replace on a failed group) additionally
        requires a justification that mentions the failed checks;
      * a ``replace`` decision must name a reading in the group;
      * accepting/replacing against a published result requires
        ``acknowledge_published`` -- the published value is never silently
        overwritten.
    """
    if decision not in ("accept", "replace", "reject"):
        raise ValueError("decision must be accept, replace, or reject")
    if group.state in TERMINAL_STATES:
        raise ValueError(f"group {group.group_id!r} is already {group.state}")
    if not reviewer.name:
        raise ValueError("reviewer identity is required")
    if not reviewer.qualifies_for(group.analyte):
        raise PermissionError(
            f"reviewer {reviewer.name!r} is not qualified to adjudicate {group.analyte!r}"
        )

    reason = (reason or "").strip()
    if decision in ("accept", "replace") and not reason:
        raise ValueError("a documented reason is required to accept or replace a result group")

    if decision == "replace":
        if not replacement_reading_id:
            raise ValueError("replace decision requires replacement_reading_id")
        if replacement_reading_id not in {r.reading_id for r in group.readings}:
            raise ValueError(f"reading {replacement_reading_id!r} is not part of the group")

    if not group.qc_passed and decision in ("accept", "replace"):
        if not reason:
            raise ValueError("cannot approve failed QC checks without a documented reason")
        named = [code for code in group.failed_checks if code in reason.lower()]
        if not named:
            raise ValueError(
                "reason must explicitly address the failed QC checks: "
                + ",".join(group.failed_checks)
            )

    republish_required = False
    if decision in ("accept", "replace"):
        if published and not acknowledge_published:
            raise PermissionError(
                "a published result cannot be silently overwritten; "
                "set acknowledge_published to supersede it with a new revision"
            )
        republish_required = published

    selected: SelectedVersion | None = None
    if decision == "accept":
        value = sum(r.value for r in group.readings) / len(group.readings)
        selected = SelectedVersion(
            group.sample,
            group.analyte,
            value,
            "duplicate_mean",
            None,
            next_revision,
            current_revision if republish_required else None,
            republish_required,
        )
    elif decision == "replace":
        chosen = next(r for r in group.readings if r.reading_id == replacement_reading_id)
        selected = SelectedVersion(
            group.sample,
            group.analyte,
            chosen.value,
            "reading",
            replacement_reading_id,
            next_revision,
            current_revision if republish_required else None,
            republish_required,
        )

    evidence = {
        "group_id": group.group_id,
        "readings": [
            {
                "reading_id": r.reading_id,
                "kind": r.kind,
                "value": r.value,
                "measured_at": r.measured_at.isoformat(),
            }
            for r in group.readings
        ],
        "rpd_pairs": [
            {
                "reading_id": p.reading_id,
                "original_value": p.original_value,
                "retest_value": p.retest_value,
                "rpd_percent": p.rpd_percent,
            }
            for p in group.rpd_pairs
        ],
        "rpd_limit_percent": group.rpd_limit_percent,
        "blank": {
            "blank_value": group.blank.blank_value,
            "blank_limit": group.blank.blank_limit,
            "passed": group.blank.passed,
        },
        "holding_time": {
            "elapsed_hours": group.holding.elapsed_hours,
            "holding_time_hours": group.holding.holding_time_hours,
            "passed": group.holding.passed,
        },
        "failed_checks": list(group.failed_checks),
        "submitted_at": group.submitted_at.isoformat(),
        "acknowledged_published": acknowledge_published if published else None,
    }

    new_state = {
        "accept": STATE_ACCEPTED,
        "replace": STATE_REPLACED,
        "reject": STATE_REJECTED,
    }[decision]

    return Adjudication(
        group_id=group.group_id,
        sample=group.sample,
        analyte=group.analyte,
        decision=new_state,
        reviewer=reviewer.name,
        reviewer_role=reviewer.role,
        reason=reason,
        selected=selected,
        failed_checks=group.failed_checks,
        qc_passed=group.qc_passed,
        published_at_submission=published,
        evidence=evidence,
        decided_at=decided_at or _now(),
    )


@dataclass
class ResultVersion:
    revision: int
    value: float
    supersedes: int | None
    published: bool
    created_at: datetime
    group_id: str | None = None


class AdjudicationLedger:
    """Append-only store for duplicate groups, decisions, and result revisions.

    Groups and decisions are never deleted -- rejected groups are listed by
    :meth:`rejected_groups` and every state change is captured in
    :meth:`audit_trail`.
    """

    def __init__(self) -> None:
        self._groups: dict[str, DuplicateGroup] = {}
        self._decisions: dict[str, Adjudication] = {}
        self._revisions: dict[tuple[str, str], list[ResultVersion]] = {}
        self._published_revisions: dict[tuple[str, str], int] = {}
        self._audit: list[AuditEvent] = []
        self._seq = 0

    # -- internal helpers -------------------------------------------------

    def _emit(self, event: str, group_id: str, at: datetime, **detail: object) -> None:
        self._seq += 1
        self._audit.append(AuditEvent(self._seq, event, group_id, at, dict(detail)))

    def _key(self, sample: str, analyte: str) -> tuple[str, str]:
        return (sample, analyte)

    def _latest_revision(self, key: tuple[str, str]) -> ResultVersion | None:
        rows = self._revisions.get(key)
        return rows[-1] if rows else None

    # -- intake ------------------------------------------------------------

    def submit(self, group: DuplicateGroup) -> DuplicateGroup:
        if group.group_id in self._groups:
            raise ValueError(f"group {group.group_id!r} has already been submitted")
        self._groups[group.group_id] = group
        self._emit(
            "submitted",
            group.group_id,
            group.submitted_at,
            sample=group.sample,
            analyte=group.analyte,
            state=group.state,
            failed_checks=list(group.failed_checks),
        )
        return group

    def publish_result(
        self,
        sample: str,
        analyte: str,
        value: float,
        published_at: datetime | None = None,
    ) -> ResultVersion:
        """Record an independently released result before any retest."""
        key = self._key(sample, analyte)
        latest = self._latest_revision(key)
        revision = (latest.revision + 1) if latest else 1
        version = ResultVersion(revision, float(value), None, True, published_at or _now())
        self._revisions.setdefault(key, []).append(version)
        self._published_revisions[key] = revision
        self._emit(
            "published",
            "",
            version.created_at,
            sample=sample,
            analyte=analyte,
            revision=revision,
            value=version.value,
        )
        return version

    # -- adjudication ------------------------------------------------------

    def adjudicate(
        self,
        group_id: str,
        reviewer: Reviewer,
        decision: str,
        reason: str,
        *,
        replacement_reading_id: str | None = None,
        acknowledge_published: bool = False,
        decided_at: datetime | None = None,
    ) -> Adjudication:
        group = self._groups.get(group_id)
        if group is None:
            raise KeyError(f"unknown group {group_id!r}")

        key = self._key(group.sample, group.analyte)
        latest = self._latest_revision(key)
        published_rev = self._published_revisions.get(key)
        # A result is "published" only when the published revision is still
        # the live one (no newer accepted/republished version exists).
        published = latest is not None and latest.revision == published_rev
        next_revision = (latest.revision + 1) if latest else 1
        current_revision = latest.revision if latest else None

        outcome = adjudicate(
            group,
            reviewer,
            decision,
            reason,
            replacement_reading_id=replacement_reading_id,
            acknowledge_published=acknowledge_published,
            published=published,
            next_revision=next_revision,
            current_revision=current_revision,
            decided_at=decided_at,
        )

        new_state = outcome.decision
        self._groups[group_id] = DuplicateGroup(
            group_id=group.group_id,
            sample=group.sample,
            analyte=group.analyte,
            original=group.original,
            retests=group.retests,
            collected_at=group.collected_at,
            blank=group.blank,
            holding=group.holding,
            rpd_limit_percent=group.rpd_limit_percent,
            rpd_pairs=group.rpd_pairs,
            failed_checks=group.failed_checks,
            state=new_state,
            submitted_at=group.submitted_at,
        )
        self._decisions[group_id] = outcome

        if outcome.selected is not None:
            version = ResultVersion(
                revision=outcome.selected.revision,
                value=outcome.selected.value,
                supersedes=outcome.selected.supersedes,
                published=False,
                created_at=outcome.decided_at,
                group_id=group_id,
            )
            self._revisions.setdefault(key, []).append(version)

        self._emit(
            "adjudicated",
            group_id,
            outcome.decided_at,
            sample=group.sample,
            analyte=group.analyte,
            decision=decision,
            reviewer=reviewer.name,
            state=new_state,
            failed_checks=list(group.failed_checks),
            republish_required=outcome.selected is not None
            and outcome.selected.republish_required,
            selected_revision=(outcome.selected.revision if outcome.selected else None),
        )
        return outcome

    def republish(
        self,
        sample: str,
        analyte: str,
        at: datetime | None = None,
    ) -> ResultVersion:
        """Release the pending revised version produced by a decision."""
        key = self._key(sample, analyte)
        latest = self._latest_revision(key)
        if latest is None:
            raise KeyError("no result exists for this sample/analyte")
        if latest.published:
            raise ValueError("current revision is already published")
        published = ResultVersion(
            revision=latest.revision,
            value=latest.value,
            supersedes=latest.supersedes,
            published=True,
            created_at=at or _now(),
            group_id=latest.group_id,
        )
        self._revisions[key][-1] = published
        self._published_revisions[key] = published.revision
        group_id = latest.group_id or ""
        self._emit(
            "republished",
            group_id,
            published.created_at,
            sample=sample,
            analyte=analyte,
            revision=published.revision,
            supersedes=published.supersedes,
            value=published.value,
        )
        return published

    # -- queries ------------------------------------------------------------

    def group(self, group_id: str) -> DuplicateGroup:
        return self._groups[group_id]

    def groups_for_review(self) -> list[DuplicateGroup]:
        return [g for g in self._groups.values() if g.state == STATE_REVIEW]

    def rejected_groups(self) -> list[DuplicateGroup]:
        return [g for g in self._groups.values() if g.state == STATE_REJECTED]

    def decision(self, group_id: str) -> Adjudication:
        return self._decisions[group_id]

    def current_result(self, sample: str, analyte: str) -> ResultVersion | None:
        return self._latest_revision(self._key(sample, analyte))

    def versions(self, sample: str, analyte: str) -> tuple[ResultVersion, ...]:
        return tuple(self._revisions.get(self._key(sample, analyte), ()))

    def audit_trail(self, group_id: str | None = None) -> tuple[AuditEvent, ...]:
        if group_id is None:
            return tuple(self._audit)
        return tuple(e for e in self._audit if e.group_id == group_id)
