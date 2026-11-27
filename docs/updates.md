# Android update and rollback contract

## Release policy

Hermes Android uses a hybrid release model:

- The APK remains the native update boundary. Android Installer handles package ID, signer and versionCode checks.
- The bundled runtime and WebUI are one immutable compatibility pair. WebUI channels resolve to exact refs: `stable` uses `v*` tags and `canary` uses `exp-v*` tags.
- `locked` uses `webui.lock.json` and `runtime.lock`; the build fails if their channels or runtime identity disagree.
- The app does not fetch or activate arbitrary live WebUI-only code.

The Build APK workflow records the runtime version/source commit, WebUI channel/tag/commit, staged WebUI digest, compatibility contract and full payload identity in `payload.json`. The release artifact also contains a receipt signed with the long-term Android release certificate.

## Payload activation

The app keeps executable payloads in app-private versioned slots:

- `files/hermes-agent-slots/<payload-id>/` — immutable candidate/active slots.
- `files/hermes-agent.active.json` — active slot, previous-good slot, generation, pending activation state, the retained-slot reuse marker and a rejected payload identity used to avoid immediately retrying a known-bad APK payload.
- `files/home/` — Hermes profiles, workspace, sessions and drafts; never replaced by a payload update.
- `files/logs/` — runtime logs.
- `databases/hermes_jobs.db` and WebView storage — outside payload slots.

Only the active and previous-good slots are retained after a healthy activation. Startup (including an ordinary no-update startup) and health completion prune older slots and abandoned staging directories, and pruning runs before the space check. If the retained previous-good already contains the requested payload, activation reuses that complete slot and does not reserve extraction space. The free-space budget is conservative additional headroom; the retained slots already consume space reflected in `usableSpace`. Cleanup failures are explicit errors, not a successful retention check.

Activation sequence:

1. Check the APK asset ZIP SHA-256 when the format-2 manifest provides it.
2. Check available storage for the candidate plus a retained previous slot.
3. Extract to a unique staging directory under `hermes-agent-slots`.
4. Apply path rewrites, symlinks and executable bits without touching the active slot.
5. Verify the launcher and write the candidate manifest.
6. Rename the complete candidate into its slot and atomically replace the active-state file.
7. Start Hermes and wait for the authenticated WebUI health probe.
8. Mark the candidate healthy only after the probe succeeds.

Verification/extraction failures before activation leave the existing active/fallback pointers intact. Service stop/destruction cancels extraction, waits for the installer worker to exit, and rolls back any pending activation instead of classifying cancellation as a payload failure. Cancelling a retained-slot reuse preserves that known-good slot for a later retry. After activation, a startup or health-state-commit failure first stops, waits for, and sweeps the candidate's process tree; only then may rollback change state and delete its directory. The failed payload identity is stored as `rejected`, never as `previous_good`. A failed cleanup or rollback-state write stops recovery without launching another runtime.

If a complete previous-good manifest/launcher exists, the same service worker starts that restored payload using its installed manifest. Subsequent starts of the same APK continue to select the restored slot; health checks of that slot do not clear the rejected APK identity. A different payload identity may be installed by a later APK. The app never repairs an old manifest with the new APK's ZIP. First-install failure, missing fallback metadata, or incomplete fallback files remain explicit errors.

On upgrade from the pre-slot layout, the legacy `files/hermes-agent` directory is first moved into a versioned slot, receives an installed manifest, and has its known absolute paths and symlink targets rewritten to the new slot path. That migrated slot can therefore serve as previous-good during the first slot-based update.

A process death before the health mark is recovered on the next service start. Recovery quiesces the failed slot's root before changing active state or deleting that slot, rather than sweeping only the restored root afterward. No user-data/database rollback is attempted.

Local regressions include the portable state model (quarantine persistence, state-write failure, process-cleanup callback ordering, repeated-update retention) and method-scoped Kotlin source-order contracts. These are static/contract evidence only: they do not execute Kotlin or validate Android process cleanup, filesystem durability, APK installation or WebView behavior on a device.

## Receipt

`update-receipt.json` is canonical JSON containing the payload manifest hash, APK hash, payload ID, runtime/WebUI identity and compatibility status. `update-receipt.sig` is an RSA-SHA256 detached signature made with the same release key used to sign the APK. `update-signing-cert.pem` is public material only. No provider credentials, cookies, tokens, workspace data or complete conversations are included.

## Compatibility status

The current contract is `android-hermes-runtime-webui-v1`:

- minimum Android API: 24;
- ABI: `arm64-v8a`;
- required WebView feature: `WEB_MESSAGE_LISTENER`;
- optional WebView feature: `DOCUMENT_START_SCRIPT`;
- status values are `verified`, `compatible-but-untested`, `incompatible` or `unknown`.

The Android container smoke test installs into `hermes-agent-slots/<payload-id>`, writes the versioned active-state file, and drives the slot-based launcher path. It is not real-device/WebView/OEM acceptance. Android 13–16, Redmi K70/HyperOS and low-storage/OEM process behavior remain parent Issue #1 acceptance work.

## Rollback rule

Do not uninstall the app or clear app data to recover from an update. APK rollback is a same-signer forward rollback build with a higher versionCode. Payload rollback uses the retained previous-good slot and does not remove `files/home`, WebView state, workspace files or the ledger.

## Integration preparation

The parent Issue #1 staging compatibility matrix and merge gate are recorded in [`docs/issue-7-integration.md`](issue-7-integration.md). It covers the capability diagnostics and JobLedger surfaces from PR #2/#3/#4 and requires them to remain additive when Issue #7 is integrated.
