# Hermes Android — Issue #14 worktree graph

## Scope and ownership

- Repository: HKLHaoBin/hermes-android.
- Worktree: D:/Dev/src/hermes-android-issue-14.
- Branch: feat/issue-14-device-matrix; existing PR #17.
- Exact review base: dc1fecfa5bf695bc28faf580105347405e279d21.
- Prior reviewed candidate: a3c3f122bbcbbc39583fa8a64378d556b4ba1d0e.
- Contributor scope is the device acceptance harness, receipt schema, focused static regressions, documentation, and existing workflow static-check wiring. Coordinator/integration worktrees are not writable from this task. No merge is authorized.

## Contract surfaces

- scripts/android_device_matrix.py defines canonical artifact/installed-package binding, explicit captured preconditions, per-test evidence envelopes, release gates, and privacy checks.
- docs/android-device-receipt.schema.json defines portable lexical/structural constraints. Runtime validation additionally enforces contextual equality, calculated dimensions/timestamps, proof contents and evidence-file hashes.
- docs/android-device-matrix.json retains 35 test IDs and five required slots, including Large-screen.
- PASS requires JSON proof bound to test, receipt, complete captured device identity, artifact identity, preconditions, timestamps and scenario measurements. Nonempty arbitrary attachments and static expected strings are insufficient.
- Large-screen requires a density-aware minimum dimension of at least 600dp; its proof binds the captured capability object. A compact high-resolution phone is not a large-screen result.
- Capture records actual installed-package presence and power state, not operator reminders. Unknown or unproved prerequisites remain blocked.
- Evidence JSON is parsed before redaction checks; raw serial keys/text, secrets, path escape and noncanonical hashes are rejected.
- docs/android-device-acceptance.md documents structured proof production and its limitations: lab/operator observations are not cryptographic physical-device attestation.

## Verified local checks for the fix candidate

Only Python/schema/source/static checks were executed. All six suites exited 0:

- scripts/test_android_device_matrix.py: 40 tests.
- scripts/test_android_device_schema.py: 10 tests.
- scripts/test_android_device_integration.py: 10 tests.
- scripts/test_job_ledger_contract.py: 7 tests.
- scripts/test_job_ledger_source.py: 4 tests.
- scripts/test_fetch_payload.py: 8 tests.
- Total: 79 tests; synthetic inputs are regression fixtures, not device receipts.
- Python compilation, validate-matrix and git diff --check also passed.
- JobLedger.kt, docs/job-ledger.md and both ledger test files match the exact review base; PR #4 content is retained.

## Remaining gates

- Independent review remains review blocked; this Contributor has not performed self-review or declared a clean review.
- No Actions run was dispatched or accepted as passing in this stage.
- No Gradle/Kotlin build, APK build/execution, container, ADB, emulator or physical-device command was run.
- Android 13–16 ARM64 hardware, OEM capture parsing, migration, memory pressure, process recreation and large-screen acceptance remain BLOCKED/NOT RUN pending authorized hardware and real scenario evidence.
- Workflow wiring and schema regression success do not establish CI/APK/device acceptance.
