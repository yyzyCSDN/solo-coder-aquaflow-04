import unittest
from datetime import datetime, timedelta, timezone

from aquaflow.lab.adjudication import (
    AdjudicationLedger,
    Reading,
    STATE_ACCEPTED,
    STATE_QC_PASSED,
    STATE_REJECTED,
    STATE_REPLACED,
    STATE_REVIEW,
    Reviewer,
    register_group,
    rpd_percent,
)
from aquaflow.workflow_service import WorkflowService

T0 = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def make_group(**overrides):
    params = dict(
        group_id="G1",
        sample="S-WELL-7",
        analyte="lead",
        original=(10.0, T0),
        retests=[(10.2, T1)],
        collected_at=T0 - timedelta(hours=2),
        blank_value=0.1,
        blank_limit=0.5,
        holding_time_hours=24.0,
        analyzed_at=T1,
        rpd_limit_percent=20.0,
    )
    params.update(overrides)
    return register_group(**params)


class RpdTests(unittest.TestCase):
    def test_rpd_formula(self):
        self.assertAlmostEqual(rpd_percent(10.0, 11.0), 9.5238095, places=5)

    def test_rpd_zero_pair_agrees(self):
        self.assertEqual(rpd_percent(0.0, 0.0), 0.0)

    def test_rpd_negative_rejected(self):
        with self.assertRaises(ValueError):
            rpd_percent(-1.0, 2.0)


class SubmissionValidationTests(unittest.TestCase):
    def test_identical_timestamps_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            make_group(retests=[(10.4, T0)])
        self.assertIn("identical measurement timestamp", str(ctx.exception))

    def test_identical_timestamps_between_retests_rejected(self):
        with self.assertRaises(ValueError):
            make_group(retests=[(10.4, T1), (10.5, T1)])

    def test_missing_retest_reading_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            make_group(retests=[])
        self.assertIn("at least one retest", str(ctx.exception))

    def test_retest_before_original_rejected(self):
        early = T0 - timedelta(minutes=30)
        with self.assertRaises(ValueError):
            make_group(retests=[(10.4, early)])

    def test_duplicate_reading_id_rejected(self):
        with self.assertRaises(ValueError):
            make_group(
                retests=[
                    Reading(10.2, T1, "retest", "dup"),
                    Reading(10.3, T2, "retest", "dup"),
                ]
            )

    def test_holding_violation_detected(self):
        g = make_group(analyzed_at=T0 + timedelta(hours=30))
        self.assertEqual(g.state, STATE_REVIEW)
        self.assertIn("holding_time", g.failed_checks)
        self.assertFalse(g.holding.passed)

    def test_blank_violation_detected(self):
        g = make_group(blank_value=0.9)
        self.assertIn("blank", g.failed_checks)
        self.assertEqual(g.state, STATE_REVIEW)

    def test_rpd_violation_detected(self):
        g = make_group(retests=[(14.0, T1)])
        self.assertIn("rpd", g.failed_checks)
        self.assertGreater(g.rpd_pairs[0].rpd_percent, g.rpd_limit_percent)

    def test_all_pass_is_not_review(self):
        g = make_group()
        self.assertEqual(g.state, STATE_QC_PASSED)
        self.assertTrue(g.qc_passed)


class AdjudicationRuleTests(unittest.TestCase):
    def setUp(self):
        self.qualified = Reviewer("qc.li", "qc_reviewer", frozenset({"lead"}))
        self.other = Reviewer("lab.wang", "analyst", frozenset({"nitrate"}))

    def test_unqualified_reviewer_denied(self):
        g = make_group(retests=[(14.0, T1)])
        with self.assertRaises(PermissionError):
            from aquaflow.lab.adjudication import adjudicate
            adjudicate(g, self.other, "accept", "rpd looks fine")

    def test_accept_without_reason_denied(self):
        g = make_group()
        from aquaflow.lab.adjudication import adjudicate
        with self.assertRaises(ValueError) as ctx:
            adjudicate(g, self.qualified, "accept", "")
        self.assertIn("reason", str(ctx.exception))

    def test_approve_failed_qc_without_reason_denied(self):
        g = make_group(blank_value=0.9)
        from aquaflow.lab.adjudication import adjudicate
        with self.assertRaises(ValueError) as ctx:
            adjudicate(g, self.qualified, "accept", "")
        self.assertIn("reason", str(ctx.exception))

    def test_approve_failed_qc_reason_must_address_checks(self):
        g = make_group(blank_value=0.9)
        from aquaflow.lab.adjudication import adjudicate
        with self.assertRaises(ValueError) as ctx:
            adjudicate(g, self.qualified, "accept", "analyst was sure")
        self.assertIn("failed QC", str(ctx.exception))
        outcome = adjudicate(
            g, self.qualified, "accept", "blank re-run clean; documenting blank deviation"
        )
        self.assertEqual(outcome.decision, STATE_ACCEPTED)

    def test_replace_requires_known_reading(self):
        g = make_group()
        from aquaflow.lab.adjudication import adjudicate
        with self.assertRaises(ValueError):
            adjudicate(g, self.qualified, "replace", "use retest", replacement_reading_id="nope")
        outcome = adjudicate(
            g,
            self.qualified,
            "replace",
            "use confirmed retest",
            replacement_reading_id="retest_1",
        )
        self.assertEqual(outcome.selected.value, 10.2)
        self.assertEqual(outcome.selected.source, "reading")

    def test_accept_uses_duplicate_mean(self):
        g = make_group(retests=[(10.2, T1), (10.4, T2)])
        from aquaflow.lab.adjudication import adjudicate
        outcome = adjudicate(g, self.qualified, "accept", "agreement within limit")
        self.assertAlmostEqual(outcome.selected.value, (10.0 + 10.2 + 10.4) / 3)

    def test_double_adjudication_denied(self):
        ledger = AdjudicationLedger()
        ledger.submit(make_group())
        ledger.adjudicate("G1", self.qualified, "accept", "fine")
        with self.assertRaises(ValueError):
            ledger.adjudicate("G1", self.qualified, "reject", "changed mind")


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.ledger = AdjudicationLedger()
        self.reviewer = Reviewer("qc.li", "qc_reviewer", frozenset({"lead"}))

    def test_review_queue_and_rejection_auditable(self):
        failed = make_group(group_id="G-FAIL", retests=[(14.0, T1)])
        clean = make_group(group_id="G-OK")
        self.ledger.submit(failed)
        self.ledger.submit(clean)
        self.assertEqual([g.group_id for g in self.ledger.groups_for_review()], ["G-FAIL"])

        decision = self.ledger.adjudicate("G-FAIL", self.reviewer, "reject", "rpd failed; re-sample")
        self.assertEqual(decision.decision, STATE_REJECTED)
        self.assertIsNone(decision.selected)

        # Rejected group stays in the store and remains auditable.
        self.assertEqual([g.group_id for g in self.ledger.rejected_groups()], ["G-FAIL"])
        self.assertEqual(self.ledger.group("G-FAIL").state, STATE_REJECTED)
        stored = self.ledger.decision("G-FAIL")
        self.assertEqual(stored.reviewer, "qc.li")
        self.assertIn("rpd", stored.evidence["failed_checks"])
        trail = self.ledger.audit_trail("G-FAIL")
        self.assertEqual([e.event for e in trail], ["submitted", "adjudicated"])

    def test_published_result_not_silently_overwritten(self):
        self.ledger.publish_result("S-WELL-7", "lead", 9.0, T0)
        self.ledger.submit(make_group())
        with self.assertRaises(PermissionError):
            self.ledger.adjudicate("G1", self.reviewer, "accept", "new readings look better")

        # Original published result is still the current live result.
        self.assertEqual(self.ledger.current_result("S-WELL-7", "lead").value, 9.0)

        # Explicit acknowledgement creates a superseding revision instead.
        outcome = self.ledger.adjudicate(
            "G1",
            self.reviewer,
            "accept",
            "new readings look better; superseding release acknowledged",
            acknowledge_published=True,
        )
        self.assertEqual(outcome.selected.revision, 2)
        self.assertEqual(outcome.selected.supersedes, 1)
        self.assertTrue(outcome.selected.republish_required)

        # Revised version exists but is not published yet -> consumers still
        # see revision 1 until formal republication.
        current = self.ledger.current_result("S-WELL-7", "lead")
        self.assertFalse(current.published)
        versions = self.ledger.versions("S-WELL-7", "lead")
        self.assertEqual([(v.revision, v.published) for v in versions], [(1, True), (2, False)])

        republished = self.ledger.republish("S-WELL-7", "lead", T2)
        self.assertEqual(republished.revision, 2)
        self.assertEqual(republished.supersedes, 1)
        self.assertTrue(republished.published)

        events = [e.event for e in self.ledger.audit_trail()]
        self.assertEqual(events, ["published", "submitted", "adjudicated", "republished"])

    def test_replace_creates_revision_with_evidence(self):
        self.ledger.submit(make_group())
        outcome = self.ledger.adjudicate(
            "G1",
            self.reviewer,
            "replace",
            "retest run on calibrated instrument",
            replacement_reading_id="retest_1",
        )
        self.assertEqual(outcome.decision, STATE_REPLACED)
        current = self.ledger.current_result("S-WELL-7", "lead")
        self.assertEqual(current.revision, 1)
        self.assertEqual(current.value, 10.2)
        self.assertEqual(current.group_id, "G1")
        evidence = self.ledger.decision("G1").evidence
        self.assertEqual(len(evidence["readings"]), 2)
        self.assertAlmostEqual(evidence["rpd_pairs"][0]["rpd_percent"], rpd_percent(10.0, 10.2))
        self.assertTrue(evidence["blank"]["passed"])
        self.assertTrue(evidence["holding_time"]["passed"])

    def test_duplicate_group_id_rejected(self):
        self.ledger.submit(make_group())
        with self.assertRaises(ValueError):
            self.ledger.submit(make_group())


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.service = WorkflowService()

    def _payload(self, **overrides):
        payload = {
            "group_id": "G1",
            "sample": "S-WELL-7",
            "analyte": "lead",
            "original": {"value": 10.0, "measured_at": T0.isoformat()},
            "retests": [{"value": 10.2, "measured_at": T1.isoformat()}],
            "collected_at": (T0 - timedelta(hours=2)).isoformat(),
            "analyzed_at": T1.isoformat(),
            "blank_value": 0.1,
            "blank_limit": 0.5,
            "holding_time_hours": 24.0,
        }
        payload.update(overrides)
        return payload

    def test_qc_workflow_pass(self):
        out = self.service.run("lab_duplicate_qc", self._payload())
        self.assertEqual(out["decision"], "pass")
        self.assertEqual(out["workflow"], "lab_duplicate_qc")
        self.assertEqual(out["evidence"]["failed_checks"], [])

    def test_qc_workflow_review_on_blank_failure(self):
        out = self.service.run("lab_duplicate_qc", self._payload(blank_value=0.9))
        self.assertEqual(out["decision"], "review")
        self.assertIn("blank", out["evidence"]["failed_checks"])

    def test_qc_workflow_identical_timestamp_rejected(self):
        payload = self._payload()
        payload["retests"] = [{"value": 10.2, "measured_at": T0.isoformat()}]
        with self.assertRaises(ValueError):
            self.service.run("lab_duplicate_qc", payload)

    def test_qc_workflow_missing_retest_rejected(self):
        with self.assertRaises(ValueError):
            self.service.run("lab_duplicate_qc", self._payload(retests=[]))

    def test_adjudication_workflow_reject(self):
        group = self._payload(retests=[{"value": 14.0, "measured_at": T1.isoformat()}])
        payload = {
            "group": group,
            "reviewer": {"name": "qc.li", "role": "qc_reviewer", "qualified_analytes": ["lead"]},
            "decision": "reject",
            "reason": "rpd failed",
        }
        out = self.service.run("lab_duplicate_adjudication", payload)
        self.assertEqual(out["decision"], STATE_REJECTED)
        self.assertIsNone(out["value"])

    def test_adjudication_workflow_failed_qc_without_reason_rejected(self):
        group = self._payload(blank_value=0.9)
        payload = {
            "group": group,
            "reviewer": {"name": "qc.li", "role": "qc_reviewer", "qualified_analytes": ["lead"]},
            "decision": "accept",
            "reason": "trust me",
        }
        with self.assertRaises(ValueError):
            self.service.run("lab_duplicate_adjudication", payload)

    def test_adjudication_workflow_unqualified_reviewer_rejected(self):
        payload = {
            "group": self._payload(),
            "reviewer": {"name": "lab.wang", "role": "analyst", "qualified_analytes": ["nitrate"]},
            "decision": "accept",
            "reason": "all good",
        }
        with self.assertRaises(PermissionError):
            self.service.run("lab_duplicate_adjudication", payload)

    def test_adjudication_workflow_published_guard(self):
        payload = {
            "group": self._payload(),
            "reviewer": {"name": "qc.li", "role": "qc_reviewer", "qualified_analytes": ["lead"]},
            "decision": "accept",
            "reason": "new run",
            "published": True,
        }
        with self.assertRaises(PermissionError):
            self.service.run("lab_duplicate_adjudication", payload)
        payload["acknowledge_published"] = True
        out = self.service.run("lab_duplicate_adjudication", payload)
        self.assertTrue(out["evidence"]["republish_required"])
        self.assertEqual(out["evidence"]["selected_revision"], 2)


if __name__ == "__main__":
    unittest.main()
