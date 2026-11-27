# Android user-space toolchain

Issue #5 adds a bounded, app-private toolchain manager. It is not a replacement for Android's system package installer and it does not grant root or shared-storage access.

Supported baseline

- Target: `linux-arm64-bionic`, ABI `arm64-v8a`, Android API 24+.
- Execution policy: the existing sideload build keeps `targetSdk = 28`; changing that policy requires a different executable-code design.
- Source authority: the Termux main `aarch64` repository is locked in `toolchain/termux-catalog.lock.json`.
- Build verification: CI downloads `InRelease`, `Packages.gz`, and the Termux keyring artifact; checks exact size/SHA-256 values; verifies the InRelease with `gpgv` and the locked key fingerprint; then emits `android/app/src/main/assets/toolchain/catalog.json`.
- The generated catalog is embedded in the APK and records the repository identity, package versions, dependencies, artifact URLs, sizes, SHA-256 values, target facts, entrypoints, PATH/library exports, source metadata, and capability assumptions.
- The locked default roots cover the Issue #5 tool inventory: `bash`, `git`, `curl`, `wget`, `tar`, `unzip`, `coreutils`, `findutils`, `ripgrep`, `ffmpeg`, `python`, `nodejs`, `npm`, and `uv`; every root and dependency is resolved from the same verified signed `aarch64` index.

Store layout

The package manager uses the app-private root `filesDir/tools`:

- `catalog.json`: the verified catalog copied from APK assets.
- `entries/<package>/<version>[-generation]/`: immutable extracted package entries; filesystem-unsafe version characters are percent-encoded in the directory name. Each entry contains a generated `.hermes-package.json` manifest with the catalog version, artifact hash, and declared entrypoints.
- `.staging/<transaction>/`: disposable extraction staging.
- `state.json`: the atomic active/previous selection and materialized facts.
- `active.json` and `previous.json`: query/debug projections.
- `facts.json`: PATH and library exports consumed by the Android runtime environment.
- `.transaction.lock`: a single-writer guard; another live writer is rejected.

Hermes user data remains under `filesDir/home` and is not inside the tool store. Cache storage is not the source of truth for installed tools or rollback state.

Operations and safety

The payload-side `android_toolchain.py` supports `inventory`, `preflight`, `install`, `update`, `remove`, `rollback`, and installed-state `env` export. `install`/`update` may receive a local archive for offline tests; without one they download only the exact HTTPS artifact URL recorded in the verified catalog and verify the response bytes before extraction.

- `preflight` rejects wrong ABI, API, libc, missing dependency, failed app-private execution/writable-root checks, or insufficient peak space before extraction.
- `install` and `update` verify the archive size and SHA-256 from the locked catalog before extraction. Archives are extracted into a transaction directory, checked for path traversal, absolute/escaping symlinks, unsupported hardlinks/device entries, and declared executable entrypoints, then activated by an atomic selection commit.
- The cross-process transaction owner records a nonce and process start identity; stale locks are reclaimed only when the recorded process identity is no longer live, and release verifies ownership before deleting the lock.
- The manager never executes archive contents during installation.
- `update` keeps the old active entry as the previous selection. Reinstalling a version never deletes a path referenced by active or previous state.
- `remove` removes the selected package and recursively drops dependencies that are no longer referenced by another active package. Immutable entry data remains available for repair/rollback.
- `rollback` swaps active and previous selections only when every previous entry is present.
- Inventory distinguishes `active`, `available`, `blocked`, `unsupported`, `outdated`, and `corrupt`; a catalog declaration is not reported as executable device capability without installed/host evidence.

Android integration

- `/__android/status.json` now contains `capabilitySchemaVersion`, the existing capability IDs, and a `toolchain` inventory object.
- `/__android/toolchain.json` returns the same machine-readable inventory without the runtime status envelope.
- `RuntimeEnv.kt` prepends active package PATH/library exports to the sealed base payload and retains explicit `/system/bin`, `/system/xbin`, and `/vendor/bin` fallbacks.
- The CI smoke launcher refreshes the same exports from installed `state.json`/`facts.json` after closure installation instead of trusting the payload manifest's static paths.
- The relocator stages the manager script and catalog into the payload manifest as `toolchain_script` and `toolchain_catalog`.

Integration contract with the Issue #1 staging branch

- Keep the parent capability boundary intact: the existing 13 capability IDs remain the only entries in `capabilities`; the toolchain is a separate versioned `toolchain` object.
- Keep `HermesRuntime.statusJson(includeToken: Boolean)` as a compatibility overload for the capability branch. The Android `AssetServer` must use the context-aware overload so the real app status includes the app-private inventory.
- The Issue #5 seam is limited to `HermesRuntime.kt`, `AssetServer.kt`, `RuntimeEnv.kt`, the payload relocator, catalog/manager scripts, workflow checks, and documentation. It does not modify `JobLedger.kt`, `HermesService.kt`, job-ledger docs, or unrelated WebUI behavior.
- When the branches are combined, preserve the capability and job-ledger workflow tests and retain the Issue #5 catalog/transaction contract test step; no local Gradle/APK/device result is implied by the source checks.

Dependency behavior

The v1 manager requires a package's declared dependencies to be active before that package can be installed. This makes missing closure visible in `preflight` instead of silently downloading an unreviewed dependency. A later UI or orchestration layer may install a catalog-locked dependency closure one package at a time; it must preserve the same preflight and transaction rules.

Out of scope

This change does not add CLI/TUI parity, skills/MCP lifecycle, browser automation, Chromium/Electron, Docker, root/system APK installation, broad storage permissions, migration/backup, job scheduling, or handset/OEM acceptance. The ARM64 Termux container smoke path is CI evidence only and must not be described as a real-device result.

Build and verification

GitHub Actions remains the build boundary. The workflow runs the pure transaction/catalog contracts, regenerates the locked catalog, stages it with the payload, and exercises catalog inventory in both `LD_LIBRARY_PATH` and RUNPATH-only container passes. APK signing and artifact verification remain unchanged.
