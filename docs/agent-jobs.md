# Durable Agent jobs

Issue #10 adds a durable Agent-job layer without creating a second ledger. `JobLedger` remains the only SQLite state machine. Runtime lifecycle records use profile `android-runtime`; Agent jobs use profile `default` and are owned by `AgentJobCoordinator`.

## Ownership

- `HermesService` owns the long-lived Hermes runtime process and its runtime lifecycle record.
- `AgentJobCoordinator` owns Agent queue selection, schedule alarms, worker lease, heartbeat, cancellation, pause/resume, and recovery.
- The coordinator worker is single-threaded and has a durable `worker_id`. Running jobs refresh `heartbeat_at` through the revision-guarded `JobLedger.heartbeat()` API.
- Agent prompt payloads are stored as app-private JSON files under `files/agent-jobs/`; the ledger stores only metadata and the payload reference.

## Scheduling and recovery

- UI handoff jobs are registered as queued records and activated after the WebUI accepts the prompt.
- Native jobs may be one-shot or periodic. Periods shorter than 60 seconds are rejected by the ledger.
- `AlarmManager` wakes the foreground service for future schedules. Due schedules are dispatched only after the Hermes runtime reports `RUNNING`.
- Process start and boot/package-replacement receivers mark missing running workers stale, surface paused/failed notifications, and re-arm future alarms.
- Queue claims use the ledger state plus `updated_at` revision. Late completion from an older worker cannot overwrite a newer state.
- `JobLedger.resume()` is the only retry path and enforces the maximum of three recovery retries.

## Notifications and diagnostics

- The runtime foreground notification remains separate from task-level notifications.
- Each Agent job gets a stable notification tag/id, a MainActivity content intent, and pause/resume/cancel actions appropriate to its state.
- The notification channel, notification permission availability, battery-optimization state, power-save mode, worker lease, heartbeat, runtime job and Agent job state are exposed through `job-diagnostics.json`.
- Notification content opens the status view and highlights the selected job when the app receives a job id intent.

## Validation boundary

The local contract tests validate the schema, state transitions, source seams, JavaScript syntax, and workflow registration. Android compilation, APK packaging, instrumentation, notification permission behavior, process death, Doze, lock-screen, and OEM battery-policy behavior remain CI/device acceptance work.
