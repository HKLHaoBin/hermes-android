# Issue #7 contributor worktree

- Worktree: `D:/Dev/src/hermes-android-issue-7`; branch: `feat/issue-7-safe-updates`.
- Delivery repository: `HKLHaoBin/hermes-android` (`build` remote); PR #16. `origin` is the read-only LeslieCoding repository, not the contributor delivery target.
- Scope: immutable runtime/WebUI identity, APK-bundled payload slot activation, previous-good rollback, release receipts and integration contracts. Do not edit coordinator/integration checkouts from this worktree.
- Entrypoints: `PayloadInstaller.kt` owns persisted slot/quarantine state and GC; `HermesService.kt` owns process quiescence and fallback startup. Their package path is `android/app/src/main/java/io/github/hermesandroid/`.
- Rollback must quiesce the failed root before any state change/deletion; a restored health check must not clear the rejected APK identity. Fallback launches use the installed manifest, never the current APK's ZIP with an older manifest.
- Executable payload retention is active plus at most one previous-good slot; user home/logs/WebView/JobLedger are outside those slots. See `docs/updates.md`.
- `scripts/issue7_integration_gate.py --require-parent` is the assembled-parent gate. Auto mode may explicitly classify a genuinely standalone contributor checkout; missing members of any detected parent surface fail closed. See `docs/issue-7-integration.md`.
- Validation boundary: Python/JS/static and portable contracts locally; no Gradle/Kotlin/Android compilation, APK, container, emulator, ADB or device execution. Static/model results do not establish Android acceptance.
