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
- `files/hermes-agent.active.json` — active slot, previous-good slot, generation and pending activation state.
- `files/home/` — Hermes profiles, workspace, sessions and drafts; never replaced by a payload update.
- `files/logs/` — runtime logs.
- `databases/hermes_jobs.db` and WebView storage — outside payload slots.

Activation sequence:

1. Check the APK asset ZIP SHA-256 when the format-2 manifest provides it.
2. Check available storage for the candidate plus a retained previous slot.
3. Extract to a unique staging directory under `hermes-agent-slots`.
4. Apply path rewrites, symlinks and executable bits without touching the active slot.
5. Verify the launcher and write the candidate manifest.
6. Rename the complete candidate into its slot and atomically replace the active-state file.
7. Start Hermes and wait for the authenticated WebUI health probe.
8. Mark the candidate healthy only after the probe succeeds.

If extraction, activation, startup or migration fails, the pending candidate is removed or rolled back to `previous_good`. A process death before the health mark is recovered on the next service start. A failed first install has no previous-good slot and remains an explicit error; it does not fabricate a successful runtime.

## Receipt

`update-receipt.json` is canonical JSON containing the payload manifest hash, APK hash, payload ID, runtime/WebUI identity and compatibility status. `update-receipt.sig` is an RSA-SHA256 detached signature made with the same release key used to sign the APK. `update-signing-cert.pem` is public material only. No provider credentials, cookies, tokens, workspace data or complete conversations are included.

## Compatibility status

The current contract is `android-hermes-runtime-webui-v1`:

- minimum Android API: 24;
- ABI: `arm64-v8a`;
- required WebView feature: `WEB_MESSAGE_LISTENER`;
- optional WebView feature: `DOCUMENT_START_SCRIPT`;
- status values are `verified`, `compatible-but-untested`, `incompatible` or `unknown`.

Android container smoke tests are not real-device/WebView/OEM acceptance. Android 13–16, Redmi K70/HyperOS and low-storage/OEM process behavior remain parent Issue #1 acceptance work.

## Rollback rule

Do not uninstall the app or clear app data to recover from an update. APK rollback is a same-signer forward rollback build with a higher versionCode. Payload rollback uses the retained previous-good slot and does not remove `files/home`, WebView state, workspace files or the ledger.

## Integration preparation

The parent Issue #1 staging compatibility matrix and merge gate are recorded in [`docs/issue-7-integration.md`](issue-7-integration.md). It covers the capability diagnostics and JobLedger surfaces from PR #2/#3/#4 and requires them to remain additive when Issue #7 is integrated.
