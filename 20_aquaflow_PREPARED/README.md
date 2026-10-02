# AquaFlow

Dependency-free municipal drinking-water treatment and distribution operations platform.

## Laboratory duplicate / split-sample adjudication

`aquaflow.lab.adjudication` implements the retest-result adjudication workflow:

- `register_group(...)` ingests an original reading plus one or more retest
  readings of the same sample/analyte. It computes the relative percent
  difference (RPD) for every original/retest pair, checks the method blank
  against its limit, and verifies the sample holding time. Any failed rule
  places the sample in the `review` state.
- Submission is rejected for identical measurement timestamps, missing retest
  readings, duplicate reading ids, or retests dated before the original.
- A qualified `Reviewer` (qualification matched by analyte) may `accept`
  (group mean), `replace` (a named reading becomes the reported value), or
  `reject` a group. Accepting/replacing requires a documented reason, and
  approving failed QC requires the reason to name the failed checks.
- `AdjudicationLedger` stores decisions, the selected revised version
  (`SelectedVersion` with revision/supersedes), and the evidence snapshot
  (readings, RPD pairs, blank and holding-time results).
- Already-published results cannot be silently overwritten: accept/replace
  requires `acknowledge_published`, creates a superseding revision that stays
  unpublished until `republish(...)`, and the previous revision remains in the
  version history. Rejected groups are retained and listed via
  `rejected_groups()`; every action is recorded in `audit_trail()`.

Stateless workflow entry points are available through `WorkflowService`:

- `lab_duplicate_qc` — register a group and return pass/review with QC evidence.
- `lab_duplicate_adjudication` — evaluate one accept/replace/reject decision.
