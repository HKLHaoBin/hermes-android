# Integration preparation: PR #2 / #3 / #4 + Issue #10

This branch is `feat/issue-10-background-scheduler`, based on merged Job Ledger commit `dc1fecfa5bf695bc28faf580105347405e279d21`. The integration reference is local `integration/issue-1-all-prs` at `9824a03`, which merges the PR #2 and PR #3 staging commits on top of the PR #4 base.

## Evidence reviewed

- PR #2 capability diagnostics: `a16f866` (`feat(android): expose capability diagnostics`).
- PR #3 Job Ledger contract: `e946e3e` (`docs(android): define job ledger contract`).
- PR #4 Job Ledger storage/revision base: merged commit `dc1fecf`; the PR branch also contains the stale-attempt fence commit `9f36fd4` after the original storage commit.
- Staging merge for PR #2: `7ffacc6`.
- Staging merge for PR #3: `9824a03`.
- Issue #10 implementation commit on this branch: `ef069ad`.

No source files in another worktree were changed during this review.

## Compatibility decisions

| Area | Staging behavior | Issue #10 branch behavior | Preparation result |
|---|---|---|---|
| Runtime status | PR #2 exposes Android API, ABI and conservative capability declarations | Runtime status additionally exposes runtime job, Agent job count and app visibility | One `HermesRuntime.statusJson()` now emits all fields together; `job_queue` is `partial`, not `unavailable` |
| Capability UI | PR #2 renders capability rows in the status view | Issue #10 already owns the Agent job and recovery cards | Capability and Agent job surfaces are separate cards and share only the status endpoint |
| Job Ledger | PR #3 defines the canonical six-state contract; PR #4 persists it in SQLite | Issue #10 adds schema v2 worker lease/heartbeat and schedule metadata | The same `JobLedger` remains the only SQLite store; no second ledger model was introduced |
| Runtime lifecycle | PR #4 runtime work uses the ledger contract for runtime metadata | Issue #10 uses profile `android-runtime` through `RuntimeJobLedger` | Runtime lifecycle and Agent jobs remain distinct from the shared ledger API |
| Agent queue | PR #2 capability declaration previously said no durable queue | Issue #10 adds queue, AlarmManager scheduling, worker lease, notifications and recovery | Capability declaration is updated to `partial`; real-device acceptance remains explicitly unverified |
| CI | Staging runs PR #2 capability and PR #3 ledger checks | Issue #10 already runs scheduler checks | Capability and integration-prep checks are now also registered in the workflow |

## Preserved invariants

- `JobLedger` remains the canonical state machine and is reused by both runtime and Agent profiles.
- `updated_at` revision fencing remains required for transitions, heartbeats and schedule changes.
- Job payloads remain outside the ledger in app-private storage.
- Runtime `runtime_id` continues to identify the non-secret payload; it is not repurposed as worker ownership.
- PR #2 capability output is conservative and does not claim CLI/TUI, plugin, MCP, browser, computer-use or real-device support.
- The status endpoint remains same-origin and continues to gate the session token behind `includeToken`.

## Checks added in this preparation

- `scripts/test_capabilities.py`: verifies the PR #2 capability set, statuses, device identity and UI labels.
- `scripts/test_integration_prep.py`: verifies that capability status, runtime/Agent job status, the canonical ledger and all CI contract checks coexist.
- Workflow now runs capability and integration-prep checks before any Android build step.

## Remaining validation boundary

This preparation intentionally does not run Gradle, APK packaging, instrumentation, emulator, real-device, Doze, lock-screen or OEM tests. Those remain CI/device acceptance work. The local checks verify source and contract compatibility only.
