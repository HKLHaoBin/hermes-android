# Issue #7 integration preparation

This branch owns safe-update identity, payload slots, receipt verification and rollback. It does not own capability diagnostics or the JobLedger implementation from Issue #1.

## Staging evidence

The local staging branch inspected for compatibility is:

- `integration/issue-1-all-prs`
- tip: `9824a03825f99038dad2f7f3d78b0bd5ea824a5c`
- first-parent history includes `dc1fecf` (`feat(android): add persistent job ledger (#4)`), the merge of `build/pr/2`, and the merge of `build/pr/3`.
- `build/pr/2` (`a16f866`) and `build/pr/3` (`e946e3e`) are ancestors of the staging tip.
- The local `build/pr/4` ref (`9f36fd4`) is not an ancestor of that staging tip; it is a later revision based on the job-ledger storage work. Do not assume that its revision-fencing changes are present unless the coordinator confirms the staging ref has been refreshed.

The staging checkout was read-only during this review.

## Compatibility contract

When Issue #7 is integrated into the staging branch, the following surfaces must be additive:

1. `HermesRuntime.statusJson()` must retain the parent capability fields (`androidApi`, `abi`, `capabilities`) while adding runtime/WebUI identity, compatibility status, active slot, previous-good slot, pending state and update error. Existing status consumers must not lose capability diagnostics.
2. The status page must retain the parent capability card and its API/ABI fields while adding the safe-update identity/rollback fields. IDs must remain unique; `app.js` must continue to call `renderCapabilities()` and render the update fields from the same status response.
3. `build-apk.yml` must retain the parent capability and JobLedger contract/source checks as well as the Issue #7 WebUI resolver, receipt, manifest and rollback checks. `issue7_integration_gate.py` admits only a genuinely unmixed standalone checkout or the complete parent contract set; a partial parent surface fails before any test runner is invoked.
4. `JobLedger.kt` remains a separate SQLite/user-data concern. Payload slot activation must not delete or replace `databases/hermes_jobs.db`, and a payload identity may be used as `runtime_id` without changing the ledger's optimistic revision contract (`expectedUpdatedAt` plus `updated_at` fencing).
5. `HermesService.kt` must keep the parent foreground-service lifecycle and job-ledger integration points when the Issue #7 startup recovery path is applied. Rollback is limited to the executable payload slot; it must not reset jobs, profile data, logs, WebView data or database state.
6. No live WebUI-only update is introduced. Stable/canary remain immutable runtime+WebUI pairs; the APK installer remains the native/app update boundary.

## Contract gate

`scripts/test_issue7_integration_contract.py` is strict when run against an integration checkout: both parent surfaces must exist and their source/workflow contracts must pass. The standalone contributor checkout runs `scripts/test_issue7_standalone_contract.py`. A partial parent surface is a failure, never a skip.

Run the standalone contract on this branch:

```text
PYTHONPATH=scripts python3 -m unittest -v scripts.test_issue7_standalone_contract
```

Run the strict contract against a staging checkout by setting `HERMES_ISSUE7_INTEGRATION_ROOT`. The current staging checkout passes the parent capability and JobLedger checks but fails the Issue #7 identity/slot check until this branch is integrated; that failure is the intended merge gate, not a claim that the unmerged staging branch already contains Issue #7.

The workflow calls `python3 scripts/issue7_integration_gate.py`. Auto mode checks all four parent files (`test_capabilities.py`, `JobLedger.kt`, `test_job_ledger_contract.py`, `test_job_ledger_source.py`), parent documentation, and capability source/UI markers. Any marker requires all four files. If none exists, the output explicitly labels the checkout `STANDALONE` and states that the parent integration gate has NOT passed. After assembly, run `python3 scripts/issue7_integration_gate.py --require-parent` to require the full parent set even if every marker has accidentally disappeared. The gate runs the strict Issue #7 integration test and all three parent Python checks and propagates the first failure.

`test_issue7_parent_gate.py` covers every nonempty incomplete combination of the four required files, an empty required-parent checkout, source/docs-only partial integration, and test-runner failure propagation. Fixture admission tests are not evidence that the actual merged Android product passed.

## Remaining integration blocker

The integration branch currently has the parent surfaces but not the Issue #7 identity/slot implementation. The coordinator must resolve the expected additive merge in `HermesRuntime.kt`, `HermesService.kt`, the status UI, and `build-apk.yml`. After the merge, rerun the integration contract test from the merged checkout. This branch does not run local Gradle, APK, container, or device tests.
