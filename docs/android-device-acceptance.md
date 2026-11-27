# Issue #14 Android 13–16 device acceptance

This document defines the durable real-device receipt and release-gate boundary for upstream Issue #14. It does not contain device results. A device is never represented by a placeholder row: the matrix contains capability slots only, and a receipt is created only from an authorized, online ADB device.

The research baseline is:

- Research receipt: `D:/Dev/src/hermes-android-issue-14-research/research.md`
- Parent issue: #1
- Target API levels: Android 13/API 33, Android 14/API 34, Android 15/API 35, Android 16/API 36
- Required ABI: `arm64-v8a`
- Receipt schema: `docs/android-device-receipt.schema.json`
- Capability matrix: `docs/android-device-matrix.json`
- Harness: `scripts/android_device_matrix.py`

## Scope and boundaries

The harness records and gates:

- exact device manufacturer, model, OEM, skin/build, ABI, Android release/API, security patch, display, IME, TalkBack, battery policy, notification permission, network backend and WebView provider/version;
- exact APK filename, size, SHA-256, application ID, version name/code and signer certificate SHA-256;
- exact Actions repository/workflow/run/head SHA;
- exact Hermes runtime version/SHA/source commit/payload ID and WebUI revision;
- one identical artifact identity across every required device receipt;
- per-test status, observation, failure class, evidence paths and evidence hashes;
- redaction state and release-gate eligibility.

The harness does not:

- install, uninstall, upgrade, downgrade or launch an APK;
- change package verifier settings, permissions, battery optimization, OEM policy or device state;
- infer a device from a model name or create a fake device row;
- accept an emulator, test endpoint, literal `unknown` identity, or incomplete device metadata as real hardware;
- treat container smoke, static tests, frontend tests or a green APK build as device acceptance;
- claim hardware results when no authorized device receipt exists.

The existing `scripts/e2e.sh` is not the release harness. In particular, it changes `package_verifier_enable`; this implementation does not call or modify that script.

## Matrix slots

`docs/android-device-matrix.json` contains only capability slots. Required M0 slots are:

| Slot | API | OEM | ABI | Required |
|---|---:|---|---|---|
| `A13-Google` | 33 | Google | `arm64-v8a` | yes |
| `A14-Samsung` | 34 | Samsung | `arm64-v8a` | yes |
| `A15-Xiaomi` | 35 | Xiaomi | `arm64-v8a` | yes |
| `A16-Google` | 36 | Google | `arm64-v8a` | yes |

`Large-screen` and `OEM-stress` are M1 optional slots for the complete Issue #1 scope. They remain absent until an actual authorized device is assigned. A slot does not contain a model, serial, fingerprint, result, or status. Static validation compares every slot's API/OEM/ABI/tier/required fields with the harness contract; swapping A13/A14 values is invalid even if aggregate API coverage still looks complete.

## Status semantics

The only permitted statuses are exactly `PASS`, `FAIL`, `BLOCKED`, and `NOT RUN`.

| Status | Meaning | Release consequence |
|---|---|---|
| `PASS` | The step ran on the authorized device, expected state was observed, and evidence exists with matching hashes. | May contribute to a passing slot. |
| `FAIL` | The step ran and observed behavior did not meet the expectation. Failure class, reason and evidence are required. | Release gate fails. |
| `BLOCKED` | The step could not run because an authorized prerequisite was absent or identity/evidence validation failed. | Release remains blocked. |
| `NOT RUN` | The step is planned but was not attempted. | Release remains blocked. |

A receipt aggregate is derived from its tests:

1. any `FAIL` -> `FAIL`;
2. otherwise any `BLOCKED` -> `BLOCKED`;
3. otherwise any `NOT RUN` -> `NOT RUN`;
4. only all `PASS` -> `PASS`.

The gate requires every M0 slot and every test ID to be `PASS`. It passes each receipt's `receipt_root` to validation and checks evidence file existence, content redaction and SHA-256 before eligibility. It also requires one identical APK/build/runtime/WebUI/payload artifact identity across all required slots. Missing receipts, duplicate slot receipts, mismatched API/OEM/ABI, invalid identity, `BLOCKED`, `NOT RUN`, missing evidence, hash mismatches or mixed artifacts are not release-ready states.

Failure classes are `app`, `service`, `job`, `notification`, `webview`, `network`, `oem`, `artifact`, `device`, `operator`, and `unknown`.

## Receipt identity

A valid receipt must include all fields defined in `docs/android-device-receipt.schema.json` and checked by the Python harness.

Device identity includes:

- `slot_id`, internal `device_id`, manufacturer, model, OEM, OEM skin/build identifier;
- `abi`, Android release, API level, build fingerprint and security patch level;
- `serial_sha256` and a short `serial_suffix`; raw `serial` is forbidden;
- `authorized=true` and an operator-supplied `authorization_ref`;
- `is_emulator=false` and a physical `transport` (`usb` or `tcpip`);
- WebView package, exact version and provider;
- display width/height/density, default IME, TalkBack state;
- battery optimization/OEM policy, notification permission and controlled backend ID.

Artifact identity includes:

- APK filename, SHA-256, byte size, application ID, version name/code and signer certificate SHA-256;
- build repository, workflow, numeric Actions run ID and 40-character head SHA;
- Actions artifact name (`apk`) bound to that exact run;
- runtime version, package SHA-256, source commit and payload ID;
- 40-character WebUI revision.

An artifact manifest can be generated from an exact APK and the identity values read from the successful Actions artifact/build receipt. The command checks an adjacent `.sha256` sidecar when present:

```text
python3 scripts/android_device_matrix.py artifact-manifest \
  --apk dist/<exact-apk>.apk \
  --output artifact.json \
  --application-id io.github.hkhaobin.hermeswebui \
  --version-name <version-name> \
  --version-code <version-code> \
  --signer-sha256 <64-hex-signer> \
  --build-run-id <actions-run-id> \
  --head-sha <40-hex-head-sha> \
  --runtime-version <runtime-version> \
  --runtime-sha256 <64-hex-runtime-sha> \
  --runtime-source-commit <40-hex-source-commit> \
  --payload-id <payload-id> \
  --webui-revision <40-hex-webui-revision> \
  --repository <owner>/<repository>
```

`--repository` is required; alternatively use `--owner <owner> --repo <repository>`. The harness has no default repository provenance, so it cannot silently record the wrong owner/repository. Angle-bracket values above are required evidence inputs, not defaults. Do not replace them with guessed values.

The CLI also accepts `--artifact-name` and `--workflow`; the Build APK workflow passes `apk` and the exact `GITHUB_WORKFLOW` value rather than relying on defaults. Validate a generated manifest before handing it to a hardware operator:

```text
python3 scripts/android_device_matrix.py validate-artifact \
  --artifact dist/artifact-manifest.json
```

## Integration-prep seam for the staged Android work

The staging branch `integration/issue-1-all-prs` contains the Android integration work from PR #2, PR #3 and PR #4. This Issue #14 branch does not turn those static or source-contract checks into hardware results; it defines the seam that the integrated APK must satisfy:

| Staged work | Existing static contract | Future real-device receipt seam |
|---|---|---|
| PR #2 capability diagnostics | `scripts/test_capabilities.py` checks `/__android/status.json`, Android API and ABI declarations. | `RD-RUNTIME-*` evidence must show the integrated diagnostics response on the exact device; a capability panel or container result is not a hardware PASS. |
| PR #3 job-ledger contract | `scripts/test_job_ledger_contract.py` checks documented fields, timestamps and transitions. | `RD-JOB-01` must run an actual integrated Agent job; `RD-JOB-02` must show recovery/retry/notification behavior after the integrated runtime wiring exists. |
| PR #4 job-ledger storage | `scripts/test_job_ledger_source.py` checks SQLite schema, profile isolation and revision-guarded transitions. | `RD-BG-*` and `RD-JOB-*` evidence must read back durable state through the app/service path on a real device; source checks alone never create a receipt. |

The Build APK workflow writes `dist/artifact-manifest.json` only after APK signature verification. It binds the exact APK filename/SHA-256/signing identity to the Actions repository, workflow, `apk` artifact name, run ID and head SHA, plus the runtime package metadata, install-stamp source commit, payload ID and pinned WebUI revision. The same `apk` Actions artifact contains the APK, its `.sha256` sidecar, release certificate and this manifest. Publish verified APK downloads that artifact by run ID, rechecks the identity and retains `artifact-manifest.json` as a release asset.

The manifest is an Actions artifact identity receipt, not a device receipt. A future authorized hardware workflow may download the `apk` artifact, validate the manifest, install and exercise that exact APK outside this static workflow, then archive one receipt directory per capability slot and run `gate`. Build CI and publish CI do not call `capture`, `record` or `gate`, do not connect to ADB, and produce no real-device result. Without an authorized online device, capture must remain `BLOCKED` with exit code `2` and write no receipt; planned but unexecuted scenarios remain `NOT RUN`.

## Redaction policy

The receipt may not contain:

- API keys, passwords, cookies, bearer tokens, authorization headers, refresh/access tokens or private keys;
- raw device serials;
- complete user messages, provider credentials or session contents.

The validator rejects secret-like field names and values, raw `serial`, bearer strings, private-key headers and common credential assignments. It scans both the receipt JSON and the bytes of every evidence file before `record` can write `redaction_checked=true`. The receipt records only `serial_sha256`, `serial_suffix`, authorization reference, structured device state and evidence hashes. Evidence files must be scrubbed before they are referenced. `raw_serial_stored=false`, `secrets_stored=false`, and `patterns_checked=true` are mandatory receipt fields.

## Hardware capture

Capture is read-only. It requires an actual online ADB device, an exact artifact manifest, an M0/M1 slot, an operator ID, an authorization reference and a controlled backend ID:

```text
python3 scripts/android_device_matrix.py capture \
  --artifact-manifest artifact.json \
  --output receipts/<slot-id>.json \
  --slot-id <slot-id> \
  --serial <authorized-adb-serial> \
  --operator-id <operator-id> \
  --authorization-ref <approved-run-reference> \
  --backend-id <controlled-backend-id>
```

The command reads only ADB metadata: `getprop`, display size/density, WebView provider, IME/accessibility settings, notification app-op, device-idle summary and airplane-mode state. It rejects `emulator-*`/test serials, emulator transport, and QEMU properties such as `ro.kernel.qemu`; it also rejects missing or literal-unknown model/release/fingerprint/security-patch/OEM-skin identity. It does not invoke `adb install`, `am start`, `settings put`, `pm grant`, `cmd deviceidle`, or any package-verifier command.

If no authorized online device is present, capture exits with code `2` and writes no receipt. That is the expected blocked behavior in environments without hardware. Multiple online devices require an explicit `--serial`.

## Recording test results

After an operator has actually run one test, record it and hash evidence files located below the receipt directory:

```text
python3 scripts/android_device_matrix.py record \
  --receipt receipts/<slot-id>.json \
  --test-id <test-id> \
  --status PASS \
  --observed <short-observed-state> \
  --reason <why-this-status-is-supported> \
  --evidence receipts/<slot-id>/status.json \
  --evidence receipts/<slot-id>/screen.png
```

Every test record requires a non-empty `--reason`; for `FAIL` or `BLOCKED`, also provide `--failure-class`. For `NOT RUN`, provide a reason such as a missing prerequisite. Evidence paths are relative in the receipt, re-hashed, and content-scanned by the validator. `record` refuses to update a receipt that is not marked as authorized hardware evidence or whose evidence contains credentials.

Validate one receipt:

```text
python3 scripts/android_device_matrix.py validate-receipt \
  --receipt receipts/<slot-id>.json \
  --check-evidence
```

## Test catalog

The harness requires every receipt to contain every test ID. The detailed test procedures and expected evidence are maintained here rather than hidden in a runner:

- Identity/install: `RD-ID-01`, `RD-INSTALL-01`, `RD-INSTALL-02`, `RD-COLD-01`, `RD-UPGRADE-01`, `RD-ROLLBACK-01`.
- Runtime/service/ledger: `RD-RUNTIME-01` through `RD-RUNTIME-05`, `RD-JOB-01`, `RD-JOB-02`.
- WebView/transport: `RD-WEB-01`, `RD-WEB-02`, `RD-STREAM-01`, `RD-STREAM-02`, `RD-TERM-01`.
- SAF/import: `RD-SAF-01` through `RD-SAF-05`.
- Background/OEM: `RD-BG-01` through `RD-BG-04`.
- Accessibility/IME/layout: `RD-UX-01` through `RD-UX-04`.

The test IDs cover clean install, cold start, same-signer upgrade, declared rollback policy, start/stop/restart races, notification permission, system stop, process death, memory pressure, actual long Agent jobs, job retry/recovery, WebView feature/origin isolation, controlled streaming, network loss/reconnect, terminal auth/CSRF, SAF providers and failures, duplicate/image imports, lock screen, battery optimization, OEM policies, task removal/reboot, TalkBack, IME, rotation, font scale, split-screen and large-screen/foldable layouts.

`RD-JOB-01` must exercise an actual integrated Agent job. Runtime health or container session creation is not a substitute for a long Agent job receipt.

## Release gate

Evaluate all receipt JSON files in a directory:

```text
python3 scripts/android_device_matrix.py gate \
  --receipts-dir receipts \
  --output receipts/release-gate.json
```

Exit codes:

- `0`: all required slots and test IDs are `PASS`.
- `1`: a malformed command/input JSON or an executed required test is `FAIL`.
- `2`: a required receipt/test is missing, invalid, `BLOCKED`, `NOT RUN`, unauthorized, or identity-mismatched.
- `64`: command usage error.

A successful APK build, signature verification, payload lock validation and Android container smoke are CI pre-gates only. They do not satisfy the hardware gate. The release gate remains blocked until every required API 33–36 slot has an authorized receipt with exact identity and every required test is `PASS`.

## CI/static boundary

The Build APK workflow runs:

```text
python3 scripts/android_device_matrix.py validate-matrix docs/android-device-matrix.json
python3 scripts/test_android_device_matrix.py
```

This is static/schema/semantic validation only. The workflow does not connect to a phone, create an emulator, run `capture`, run `record`, alter package verifier settings, or publish hardware results. The workflow continues to use the existing runtime lock, WebUI pin, signing, artifact and container-smoke conventions.

Hardware execution is an authorized operator/lab responsibility. A later hardware workflow may consume the exact Actions artifact and this harness, but it must provide real ADB devices and archive the receipts; it must not manufacture missing slots or convert `BLOCKED`/`NOT RUN` into `PASS`.
