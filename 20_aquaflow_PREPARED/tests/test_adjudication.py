import unittest
from datetime import datetime, timedelta, timezone

from aquaflow.api import AquaFlowService
from aquaflow.lab.adjudication import (
    ACCEPT, ACCEPTED, Ledger, Limits, REJECT, REJECTED, REPLACE, REVIEW,
    Reading, adjudicate, current, get, history, in_review, rejected, rpd, submit,
)

BASE = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
LIMITS = Limits(max_rpd_percent=5.0, blank_limit=0.1, max_holding_hours=24.0)
REVIEWERS = frozenset({"qa-lead", "qa-manager"})


def reading(value, hours=2.0, analyst="analyst-1", sample="S1", analyte="chlorine", kind="duplicate"):
    return Reading(sample, analyte, value, analyst, BASE + timedelta(hours=hours), kind)


def pair(v1=1.00, v2=1.02, **kw):
    return [reading(v1, hours=2.0, **kw), reading(v2, hours=3.0, analyst="analyst-2", **kw)]


def submit_pair(ledger, set_id, v1=1.00, v2=1.02, blank=0.05, limits=LIMITS, **kw):
    return submit(ledger, set_id, pair(v1, v2, **kw), blank, BASE, limits,
                  BASE + timedelta(hours=4))


class RpdTest(unittest.TestCase):
    def test_rpd(self):
        self.assertAlmostEqual(rpd(10.0, 12.0), 2.0 / 11.0 * 100.0)

    def test_rpd_zero_pair(self):
        self.assertEqual(rpd(0.0, 0.0), 0.0)

    def test_rpd_negative(self):
        with self.assertRaises(ValueError):
            rpd(-1.0, 2.0)


class SubmitTest(unittest.TestCase):
    def test_qc_pass_auto_accepts_and_publishes(self):
        ledger = submit_pair(Ledger(), "set-1")
        result_set = get(ledger, "set-1")
        self.assertEqual(result_set.state, ACCEPTED)
        self.assertEqual(result_set.revision, 1)
        self.assertIsNone(result_set.supersedes)
        published = current(ledger, "S1", "chlorine")
        self.assertEqual(published.revision, 1)
        self.assertAlmostEqual(published.value, 1.01)
        self.assertEqual(published.published_by, "system")

    def test_qc_failure_holds_set_in_review(self):
        ledger = submit_pair(Ledger(), "set-1", 1.00, 1.40)  # rpd ~33%
        result_set = get(ledger, "set-1")
        self.assertEqual(result_set.state, REVIEW)
        self.assertIn("set-1", [s.set_id for s in in_review(ledger)])
        self.assertIsNone(current(ledger, "S1", "chlorine"))
        failed = [c.name for c in result_set.checks if not c.passed]
        self.assertEqual(failed, ["rpd"])

    def test_blank_failure_holds_set(self):
        ledger = submit_pair(Ledger(), "set-1", blank=0.5)
        self.assertEqual(get(ledger, "set-1").state, REVIEW)

    def test_holding_time_failure_holds_set(self):
        late = [reading(1.00, hours=30.0), reading(1.01, hours=31.0, analyst="analyst-2")]
        ledger = submit(Ledger(), "set-1", late, 0.05, BASE, LIMITS, BASE + timedelta(hours=32))
        result_set = get(ledger, "set-1")
        self.assertEqual(result_set.state, REVIEW)
        self.assertFalse([c for c in result_set.checks if c.name == "holding_time"][0].passed)

    def test_missing_replicate_readings_rejected(self):
        with self.assertRaises(ValueError):
            submit(Ledger(), "set-1", [reading(1.0)], 0.05, BASE, LIMITS,
                   BASE + timedelta(hours=4))

    def test_identical_timestamps_rejected(self):
        stamp = BASE + timedelta(hours=2)
        rows = [Reading("S1", "chlorine", 1.00, "analyst-1", stamp),
                Reading("S1", "chlorine", 1.01, "analyst-2", stamp)]
        with self.assertRaises(ValueError):
            submit(Ledger(), "set-1", rows, 0.05, BASE, LIMITS, BASE + timedelta(hours=4))

    def test_mixed_samples_rejected(self):
        rows = [reading(1.00), reading(1.01, hours=3.0, analyst="analyst-2", sample="S2")]
        with self.assertRaises(ValueError):
            submit(Ledger(), "set-1", rows, 0.05, BASE, LIMITS, BASE + timedelta(hours=4))

    def test_reading_before_collection_rejected(self):
        rows = [reading(1.00, hours=-1.0), reading(1.01, hours=3.0, analyst="analyst-2")]
        with self.assertRaises(ValueError):
            submit(Ledger(), "set-1", rows, 0.05, BASE, LIMITS, BASE + timedelta(hours=4))

    def test_duplicate_set_id_rejected(self):
        ledger = submit_pair(Ledger(), "set-1")
        with self.assertRaises(ValueError):
            submit_pair(ledger, "set-1")


class AdjudicateTest(unittest.TestCase):
    def failing_ledger(self):
        return submit_pair(Ledger(), "set-1", 1.00, 1.40)  # rpd failure -> review

    def test_accept_with_reason_publishes_new_revision(self):
        ledger = self.failing_ledger()
        at = BASE + timedelta(hours=6)
        ledger = adjudicate(ledger, "set-1", ACCEPT, "qa-lead", REVIEWERS, at,
                            reason="confirmed by re-analysis")
        result_set = get(ledger, "set-1")
        self.assertEqual(result_set.state, ACCEPTED)
        self.assertEqual(result_set.decision.reviewer, "qa-lead")
        self.assertEqual(result_set.decision.revision, 1)
        self.assertEqual([c.name for c in result_set.decision.evidence],
                         ["rpd", "blank", "holding_time"])
        self.assertAlmostEqual(current(ledger, "S1", "chlorine").value, 1.20)

    def test_approve_failed_qc_without_reason_rejected(self):
        ledger = self.failing_ledger()
        at = BASE + timedelta(hours=6)
        for action in (ACCEPT, REPLACE):
            with self.assertRaises(ValueError):
                adjudicate(ledger, "set-1", action, "qa-lead", REVIEWERS, at,
                           reason="", value=1.25)
            with self.assertRaises(ValueError):
                adjudicate(ledger, "set-1", action, "qa-lead", REVIEWERS, at,
                           reason="   ", value=1.25)

    def test_reject_without_reason_rejected(self):
        ledger = self.failing_ledger()
        with self.assertRaises(ValueError):
            adjudicate(ledger, "set-1", REJECT, "qa-lead", REVIEWERS,
                       BASE + timedelta(hours=6))

    def test_unqualified_reviewer_rejected(self):
        ledger = self.failing_ledger()
        with self.assertRaises(ValueError):
            adjudicate(ledger, "set-1", ACCEPT, "intern", REVIEWERS,
                       BASE + timedelta(hours=6), reason="looks fine")

    def test_reviewer_must_not_be_analyst(self):
        ledger = self.failing_ledger()
        with self.assertRaises(ValueError):
            adjudicate(ledger, "set-1", ACCEPT, "analyst-1",
                       REVIEWERS | {"analyst-1"}, BASE + timedelta(hours=6),
                       reason="self approval")

    def test_replace_publishes_replacement_value(self):
        ledger = self.failing_ledger()
        ledger = adjudicate(ledger, "set-1", REPLACE, "qa-lead", REVIEWERS,
                            BASE + timedelta(hours=6), reason="confirmatory run",
                            value=1.05)
        result_set = get(ledger, "set-1")
        self.assertEqual(result_set.state, ACCEPTED)
        self.assertEqual(result_set.decision.action, REPLACE)
        published = current(ledger, "S1", "chlorine")
        self.assertEqual(published.value, 1.05)
        self.assertEqual(published.published_by, "qa-lead")

    def test_replace_requires_value(self):
        ledger = self.failing_ledger()
        with self.assertRaises(ValueError):
            adjudicate(ledger, "set-1", REPLACE, "qa-lead", REVIEWERS,
                       BASE + timedelta(hours=6), reason="confirmatory run")

    def test_rejected_set_stays_auditable_and_unpublished(self):
        ledger = self.failing_ledger()
        ledger = adjudicate(ledger, "set-1", REJECT, "qa-lead", REVIEWERS,
                            BASE + timedelta(hours=6), reason="blank contaminated")
        result_set = get(ledger, "set-1")
        self.assertEqual(result_set.state, REJECTED)
        self.assertEqual(result_set.decision.reason, "blank contaminated")
        self.assertEqual([s.set_id for s in rejected(ledger)], ["set-1"])
        self.assertIsNone(current(ledger, "S1", "chlorine"))
        self.assertEqual(ledger.published, ())

    def test_decided_set_cannot_be_adjudicated_again(self):
        ledger = self.failing_ledger()
        at = BASE + timedelta(hours=6)
        ledger = adjudicate(ledger, "set-1", REJECT, "qa-lead", REVIEWERS, at,
                            reason="invalid run")
        with self.assertRaises(ValueError):
            adjudicate(ledger, "set-1", ACCEPT, "qa-lead", REVIEWERS,
                       at + timedelta(hours=1), reason="second thoughts")

    def test_time_reversal_rejected(self):
        ledger = self.failing_ledger()
        with self.assertRaises(ValueError):
            adjudicate(ledger, "set-1", ACCEPT, "qa-lead", REVIEWERS,
                       BASE + timedelta(hours=1), reason="too early")

    def test_unknown_action_and_set(self):
        ledger = self.failing_ledger()
        at = BASE + timedelta(hours=6)
        with self.assertRaises(ValueError):
            adjudicate(ledger, "set-1", "ignore", "qa-lead", REVIEWERS, at, reason="x")
        with self.assertRaises(KeyError):
            adjudicate(ledger, "nope", ACCEPT, "qa-lead", REVIEWERS, at, reason="x")


class PublicationTest(unittest.TestCase):
    def test_published_result_not_silently_rewritten(self):
        ledger = submit_pair(Ledger(), "set-1", 1.00, 1.02)          # publishes rev 1
        ledger = submit_pair(ledger, "set-2", 1.10, 1.12)            # publishes rev 2
        revisions = history(ledger, "S1", "chlorine")
        self.assertEqual([p.revision for p in revisions], [1, 2])
        self.assertEqual(revisions[1].supersedes, 1)
        self.assertAlmostEqual(revisions[0].value, 1.01)             # rev 1 preserved
        self.assertAlmostEqual(current(ledger, "S1", "chlorine").value, 1.11)

    def test_adjudicated_accept_supersedes_explicitly(self):
        ledger = submit_pair(Ledger(), "set-1", 1.00, 1.02)          # rev 1 published
        ledger = submit_pair(ledger, "set-2", 1.00, 1.40)            # rpd fail -> review
        ledger = adjudicate(ledger, "set-2", ACCEPT, "qa-lead", REVIEWERS,
                            BASE + timedelta(hours=6), reason="verified by referee lab")
        revisions = history(ledger, "S1", "chlorine")
        self.assertEqual([p.revision for p in revisions], [1, 2])
        self.assertEqual(revisions[1].supersedes, 1)
        self.assertEqual(revisions[1].set_id, "set-2")
        self.assertAlmostEqual(current(ledger, "S1", "chlorine").value, 1.20)
        self.assertEqual(get(ledger, "set-2").decision.revision, 2)

    def test_rejected_duplicate_leaves_published_result_untouched(self):
        ledger = submit_pair(Ledger(), "set-1", 1.00, 1.02)          # rev 1 published
        ledger = submit_pair(ledger, "set-2", 2.00, 2.80)            # rpd fail -> review
        ledger = adjudicate(ledger, "set-2", REJECT, "qa-lead", REVIEWERS,
                            BASE + timedelta(hours=6), reason="contaminated glassware")
        self.assertEqual([p.revision for p in history(ledger, "S1", "chlorine")], [1])
        self.assertAlmostEqual(current(ledger, "S1", "chlorine").value, 1.01)
        self.assertEqual([s.set_id for s in rejected(ledger)], ["set-2"])


class WorkflowTest(unittest.TestCase):
    def payload(self, v1, v2, blank=0.05):
        return {
            "readings": [
                {"sample": "S1", "analyte": "chlorine", "value": v1,
                 "analyst": "analyst-1", "measured_at": "2026-10-01T10:00:00+00:00"},
                {"sample": "S1", "analyte": "chlorine", "value": v2,
                 "analyst": "analyst-2", "measured_at": "2026-10-01T11:00:00+00:00",
                 "kind": "split"},
            ],
            "blank": blank,
            "collected_at": "2026-10-01T08:00:00+00:00",
            "limits": {"max_rpd_percent": 5.0, "blank_limit": 0.1,
                       "max_holding_hours": 24.0},
        }

    def test_workflow_accepts_clean_set(self):
        result = AquaFlowService().run_workflow(
            "lab_duplicate_adjudication", self.payload(1.00, 1.02))
        self.assertEqual(result["decision"], "accept")
        self.assertEqual(result["evidence"]["failed"], [])
        self.assertTrue(result["evidence"]["checks"]["rpd"]["passed"])
        self.assertEqual(result["audit"]["contract"], "v1")

    def test_workflow_routes_failed_set_to_review(self):
        result = AquaFlowService().run_workflow(
            "lab_duplicate_adjudication", self.payload(1.00, 1.40, blank=0.5))
        self.assertEqual(result["decision"], "review")
        self.assertEqual(result["evidence"]["failed"], ["blank", "rpd"])

    def test_workflow_requires_fields(self):
        with self.assertRaises(ValueError):
            AquaFlowService().run_workflow("lab_duplicate_adjudication", {"readings": []})


if __name__ == "__main__":
    unittest.main()
