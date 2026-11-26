package io.github.hermesandroid

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import org.json.JSONArray
import org.json.JSONObject
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Canonical owner for Agent jobs. Runtime lifecycle records use a separate profile;
 * this coordinator only owns durable Agent records and their app-private payloads.
 */
object AgentJobCoordinator {
    const val AGENT_PROFILE = "default"

    private const val HEARTBEAT_INTERVAL_MS = 5_000L
    private const val TICK_INTERVAL_MS = 2_000L
    private const val UI_HANDOFF_TIMEOUT_MS = 60_000L
    private const val ALARM_MAX = Long.MAX_VALUE

    private val lock = Any()
    private val actions = ConcurrentHashMap<String, JobAction>()
    private val executing = AtomicBoolean(false)

    @Volatile private var appContext: Context? = null
    @Volatile private var ledger: JobLedger? = null
    @Volatile private var payloadStore: AgentJobPayloadStore? = null
    @Volatile private var executor: ScheduledExecutorService? = null
    @Volatile private var workerId: String = ""
    @Volatile private var runtime = HermesRuntime.Snapshot()
    @Volatile private var activeJobId: String? = null
    @Volatile private var activeHeartbeatAt: Long? = null
    @Volatile private var lastRecoveryAt: Long = 0L

    fun initialize(context: Context) {
        val value = context.applicationContext
        if (appContext === value) return
        synchronized(lock) {
            if (appContext === value) return
            appContext = value
            ledger = JobLedger(value)
            payloadStore = AgentJobPayloadStore(value)
        }
    }

    fun onRuntimeReady(snapshot: HermesRuntime.Snapshot) {
        runtime = snapshot
        startWorker(appContext ?: return)
        executor?.execute { tick() }
    }

    fun onRuntimeUnavailable(snapshot: HermesRuntime.Snapshot = HermesRuntime.snapshot) {
        runtime = snapshot
        val current = activeJobId
        if (current != null) actions.putIfAbsent(current, JobAction.PAUSE)
    }

    fun onServiceDestroyed() {
        onRuntimeUnavailable(runtime.copy(phase = HermesRuntime.Phase.STOPPED, message = "runtime service destroyed"))
        synchronized(lock) {
            executor?.shutdownNow()
            executor = null
            workerId = ""
            activeJobId = null
            activeHeartbeatAt = null
            executing.set(false)
        }
    }

    fun registerUiJob(sessionId: String, prompt: String, profileId: String = AGENT_PROFILE): String {
        return createJob(
            profileId = profileId,
            sessionId = sessionId,
            prompt = prompt,
            mode = AgentJobMode.UI_HANDOFF,
            nextRunAt = ALARM_MAX,
            intervalMs = null,
        )
    }

    fun activateJob(jobId: String, profileId: String = AGENT_PROFILE): Boolean {
        initialize(appContext ?: return false)
        val jobLedger = ledger ?: return false
        val current = jobLedger.get(profileId, jobId) ?: return false
        if (current.state != JobState.QUEUED) return false
        val schedule = jobLedger.scheduleForJob(profileId, jobId) ?: return false
        val changed = jobLedger.setScheduleDue(profileId, schedule.scheduleId, schedule.updatedAt, System.currentTimeMillis())
        if (changed == null) return false
        scheduleAlarm(changed)
        wakeService()
        return true
    }

    fun scheduleNativeJob(
        sessionId: String,
        prompt: String,
        delayMs: Long = 0L,
        intervalMs: Long? = null,
        profileId: String = AGENT_PROFILE,
    ): String {
        val delay = delayMs.coerceAtLeast(0L)
        return createJob(
            profileId = profileId,
            sessionId = sessionId,
            prompt = prompt,
            mode = AgentJobMode.NATIVE,
            nextRunAt = System.currentTimeMillis() + delay,
            intervalMs = intervalMs,
        )
    }

    fun resumeJob(jobId: String, profileId: String = AGENT_PROFILE): Boolean {
        initialize(appContext ?: return false)
        val jobLedger = ledger ?: return false
        val resumed = runCatching { jobLedger.resume(profileId, jobId) }.getOrNull() ?: return false
        if (resumed.state != JobState.QUEUED) {
            AgentJobNotification.show(appContext!!, resumed)
            return false
        }
        val schedule = jobLedger.scheduleForJob(profileId, jobId)
        val updated = if (schedule == null) {
            jobLedger.createSchedule(profileId, jobId, jobId, System.currentTimeMillis())
        } else {
            jobLedger.setScheduleDue(profileId, schedule.scheduleId, schedule.updatedAt, System.currentTimeMillis())
                ?: return false
        }
        AgentJobNotification.show(appContext!!, updatedJob(resumed, notificationId = resumed.notificationId))
        scheduleAlarm(updated)
        wakeService()
        publishAgentSummary()
        return true
    }

    fun pauseJob(jobId: String, profileId: String = AGENT_PROFILE): Boolean {
        initialize(appContext ?: return false)
        val current = ledger?.get(profileId, jobId) ?: return false
        return when (current.state) {
            JobState.RUNNING -> {
                actions[jobId] = JobAction.PAUSE
                true
            }
            JobState.QUEUED -> {
                val paused = ledger?.transition(
                    profileId = profileId,
                    jobId = jobId,
                    expected = JobState.QUEUED,
                    expectedUpdatedAt = current.updatedAt,
                    next = JobState.PAUSED,
                    error = "用户暂停了后台任务",
                ) ?: return false
                ledger?.scheduleForJob(profileId, jobId)?.let { schedule ->
                    ledger?.setScheduleDue(profileId, schedule.scheduleId, schedule.updatedAt, ALARM_MAX, enabled = false)
                    cancelAlarm(jobId)
                }
                AgentJobNotification.show(appContext!!, paused)
                publishAgentSummary()
                true
            }
            JobState.PAUSED, JobState.FAILED, JobState.SUCCEEDED, JobState.CANCELLED -> false
        }
    }

    fun cancelJob(jobId: String, profileId: String = AGENT_PROFILE): Boolean {
        initialize(appContext ?: return false)
        val jobLedger = ledger ?: return false
        val current = jobLedger.get(profileId, jobId) ?: return false
        if (current.state == JobState.RUNNING) {
            actions[jobId] = JobAction.CANCEL
            return true
        }
        if (!JobLedger.canTransition(current.state, JobState.CANCELLED)) return false
        val cancelled = jobLedger.transition(
            profileId = profileId,
            jobId = jobId,
            expected = current.state,
            expectedUpdatedAt = current.updatedAt,
            next = JobState.CANCELLED,
        ) ?: return false
        jobLedger.scheduleForJob(profileId, jobId)?.let { schedule ->
            jobLedger.setScheduleDue(profileId, schedule.scheduleId, schedule.updatedAt, ALARM_MAX, enabled = false)
            cancelAlarm(jobId)
        }
        AgentJobNotification.show(appContext!!, cancelled)
        publishAgentSummary()
        return true
    }

    fun onAlarm(context: Context, jobId: String, profileId: String = AGENT_PROFILE) {
        initialize(context)
        val schedule = ledger?.scheduleForJob(profileId, jobId)
        if (schedule != null && schedule.enabled && schedule.nextRunAt > System.currentTimeMillis()) {
            scheduleAlarm(schedule)
            return
        }
        wakeService()
    }

    fun restoreAfterProcessStart(context: Context, wakeIfDue: Boolean = true) {
        initialize(context)
        val jobLedger = ledger ?: return
        lastRecoveryAt = System.currentTimeMillis()
        runCatching { jobLedger.markStale(AGENT_PROFILE, emptySet(), lastRecoveryAt) }
        jobLedger.list(AGENT_PROFILE, 200)
            .filter { it.state == JobState.PAUSED || it.state == JobState.FAILED }
            .forEach { AgentJobNotification.show(context, it) }
        publishAgentSummary()
        rescheduleAlarms()
        if (wakeIfDue && jobLedger.dueSchedules().isNotEmpty()) wakeService()
    }

    fun recoverAfterProcessDeath(context: Context) {
        restoreAfterProcessStart(context, wakeIfDue = false)
    }

    fun jobsJson(profileId: String = AGENT_PROFILE): String {
        initialize(appContext ?: return JSONObject().put("jobs", JSONArray()).toString())
        val jobLedger = ledger ?: return JSONObject().put("jobs", JSONArray()).toString()
        publishAgentSummary()
        val jobs = JSONArray()
        jobLedger.list(profileId, 200).forEach { record ->
            val schedule = jobLedger.scheduleForJob(profileId, record.jobId)
            jobs.put(jobJson(record, schedule))
        }
        return JSONObject().put("profile", profileId).put("jobs", jobs).toString()
    }

    fun jobDiagnosticsJson(): String {
        val context = appContext ?: return JSONObject().put("initialized", false).toString()
        initialize(context)
        val jobLedger = ledger ?: return JSONObject().put("initialized", false).toString()
        publishAgentSummary()
        val schedules = jobLedger.dueSchedules(limit = 200)
        val nextRun = jobLedger.list(AGENT_PROFILE, 200)
            .mapNotNull { jobLedger.scheduleForJob(AGENT_PROFILE, it.jobId) }
            .filter { it.enabled && it.nextRunAt != ALARM_MAX }
            .minOfOrNull { it.nextRunAt }
        val active = activeJobId?.let { jobLedger.get(AGENT_PROFILE, it) }
        return JSONObject().apply {
            put("initialized", true)
            put("workerAlive", executor != null && !executor!!.isShutdown)
            put("workerId", workerId)
            put("activeJobId", activeJobId ?: JSONObject.NULL)
            put("heartbeatAt", activeHeartbeatAt ?: JSONObject.NULL)
            put("appVisible", runtime.appVisible)
            put("runtimePhase", runtime.phase.name.lowercase())
            put("runtimeJobId", runtime.runtimeJobId)
            put("runtimeJobState", runtime.runtimeJobState)
            put("nextRunAt", nextRun ?: JSONObject.NULL)
            put("dueSchedules", schedules.size)
            put("lastRecoveryAt", lastRecoveryAt)
            put("activeJobState", active?.state?.value ?: JSONObject.NULL)
            put("activeJobWorkerId", active?.workerId ?: JSONObject.NULL)
            put("notification", AgentJobNotification.channelDiagnostics(context))
        }.toString()
    }

    private fun createJob(
        profileId: String,
        sessionId: String,
        prompt: String,
        mode: AgentJobMode,
        nextRunAt: Long,
        intervalMs: Long?,
    ): String {
        require(sessionId.isNotBlank()) { "sessionId must not be blank" }
        require(prompt.isNotBlank()) { "prompt must not be blank" }
        initialize(appContext ?: error("AgentJobCoordinator is not initialized"))
        val context = appContext!!
        val jobLedger = ledger!!
        val jobId = UUID.randomUUID().toString()
        val notificationId = "agent:$jobId"
        val payload = AgentJobPayload(profileId, sessionId, prompt, mode, System.currentTimeMillis())
        payloadStore!!.save(jobId, payload)
        try {
            val record = jobLedger.create(
                profileId = profileId,
                jobId = jobId,
                sessionId = sessionId,
                notificationId = notificationId,
            )
            val schedule = jobLedger.createSchedule(
                profileId = profileId,
                jobId = jobId,
                payloadRef = jobId,
                nextRunAt = nextRunAt,
                intervalMs = intervalMs,
            )
            AgentJobNotification.show(context, record)
            if (nextRunAt != ALARM_MAX) {
                scheduleAlarm(schedule)
                if (nextRunAt <= System.currentTimeMillis()) wakeService()
            }
            publishAgentSummary()
            return jobId
        } catch (error: Throwable) {
            payloadStore!!.delete(jobId)
            throw error
        }
    }

    private fun startWorker(context: Context) {
        initialize(context)
        synchronized(lock) {
            if (executor != null && !executor!!.isShutdown) return
            workerId = "agent-worker-${UUID.randomUUID()}"
            executor = Executors.newSingleThreadScheduledExecutor { runnable ->
                Thread(runnable, "hermes-agent-worker").apply { isDaemon = true }
            }
            executor!!.scheduleWithFixedDelay({
                try {
                    tick()
                } catch (_: Throwable) {
                }
            }, 0L, TICK_INTERVAL_MS, TimeUnit.MILLISECONDS)
        }
    }

    private fun tick() {
        if (runtime.phase != HermesRuntime.Phase.RUNNING || executing.get()) return
        val jobLedger = ledger ?: return
        val schedules = jobLedger.dueSchedules(limit = 20)
        for (schedule in schedules) {
            val record = jobLedger.get(schedule.profileId, schedule.jobId) ?: continue
            if (record.state != JobState.QUEUED) {
                if (record.state.isTerminal || record.state == JobState.PAUSED || record.state == JobState.FAILED) {
                    jobLedger.setScheduleDue(schedule.profileId, schedule.scheduleId, schedule.updatedAt, ALARM_MAX, enabled = false)
                }
                continue
            }
            val payload = payloadStore?.load(schedule.payloadRef)
            if (payload == null) {
                val failed = jobLedger.transition(
                    profileId = record.profileId,
                    jobId = record.jobId,
                    expected = JobState.QUEUED,
                    expectedUpdatedAt = record.updatedAt,
                    next = JobState.FAILED,
                    error = "storage payload missing",
                )
                failed?.let { AgentJobNotification.show(appContext!!, it) }
                publishAgentSummary()
                continue
            }
            if (!executing.compareAndSet(false, true)) return
            executor?.execute {
                try {
                    execute(schedule, record, payload)
                } finally {
                    executing.set(false)
                    activeJobId = null
                    activeHeartbeatAt = null
                }
            }
            return
        }
    }

    private fun execute(schedule: JobSchedule, initial: JobRecord, payload: AgentJobPayload) {
        val context = appContext ?: return
        val jobLedger = ledger ?: return
        activeJobId = initial.jobId
        var current = initial
        var streamId: String? = null
        try {
            if (payload.mode == AgentJobMode.UI_HANDOFF) {
                val deadline = System.currentTimeMillis() + UI_HANDOFF_TIMEOUT_MS
                while (System.currentTimeMillis() < deadline) {
                    if (actions[current.jobId] == JobAction.CANCEL) return finishCancelled(current, streamId, payload)
                    val status = AgentJobClient { runtime }.sessionStatus(payload.sessionId)
                    if (status.running) {
                        streamId = status.streamId
                        break
                    }
                    Thread.sleep(1_000L)
                }
                if (streamId == null && !runCatching {
                        AgentJobClient { runtime }.sessionStatus(payload.sessionId).running
                    }.getOrDefault(false)) {
                    return finishFailedQueued(current, "Agent 任务未在 UI handoff 窗口内启动")
                }
                current = jobLedger.transition(
                    profileId = current.profileId,
                    jobId = current.jobId,
                    expected = JobState.QUEUED,
                    expectedUpdatedAt = current.updatedAt,
                    next = JobState.RUNNING,
                    workerId = workerId,
                ) ?: return
                AgentJobNotification.show(context, current)
                publishAgentSummary()
            } else {
                current = jobLedger.transition(
                    profileId = current.profileId,
                    jobId = current.jobId,
                    expected = JobState.QUEUED,
                    expectedUpdatedAt = current.updatedAt,
                    next = JobState.RUNNING,
                    workerId = workerId,
                ) ?: return
                AgentJobNotification.show(context, current)
                publishAgentSummary()
                streamId = AgentJobClient { runtime }.start(payload)
            }
            var lastHeartbeat = System.currentTimeMillis()
            while (true) {
                val action = actions.remove(current.jobId)
                if (action == JobAction.CANCEL) return finishCancelled(current, streamId, payload)
                if (action == JobAction.PAUSE) return finishPaused(current, streamId, payload)
                if (runtime.phase != HermesRuntime.Phase.RUNNING) return finishPaused(current, streamId, payload)
                val status = AgentJobClient { runtime }.sessionStatus(payload.sessionId)
                if (!status.running) return finishSucceeded(current, schedule, payload)
                if (status.streamId != null) streamId = status.streamId
                val now = System.currentTimeMillis()
                if (now - lastHeartbeat >= HEARTBEAT_INTERVAL_MS) {
                    current = jobLedger.heartbeat(current.profileId, current.jobId, current.updatedAt, workerId, now)
                        ?: return
                    activeHeartbeatAt = current.heartbeatAt
                    lastHeartbeat = now
                }
                Thread.sleep(1_000L)
            }
        } catch (error: Throwable) {
            if (runtime.phase != HermesRuntime.Phase.RUNNING) finishPaused(current, streamId, payload)
            else finishFailed(current, error.message ?: error.javaClass.simpleName)
        }
    }

    private fun finishSucceeded(current: JobRecord, schedule: JobSchedule, payload: AgentJobPayload): JobRecord? {
        val jobLedger = ledger ?: return null
        if (actions.remove(current.jobId) == JobAction.CANCEL) {
            return finishCancelled(current, null, payload)
        }
        val succeeded = jobLedger.transition(
            profileId = current.profileId,
            jobId = current.jobId,
            expected = JobState.RUNNING,
            expectedUpdatedAt = current.updatedAt,
            next = JobState.SUCCEEDED,
            workerId = workerId,
        ) ?: return null
        AgentJobNotification.show(appContext!!, succeeded)
        publishAgentSummary()
        val interval = schedule.intervalMs
        if (interval != null) {
            val nextJobId = UUID.randomUUID().toString()
            payloadStore!!.save(nextJobId, payload.copy(createdAt = System.currentTimeMillis()))
            val next = jobLedger.create(
                profileId = current.profileId,
                jobId = nextJobId,
                sessionId = payload.sessionId,
                notificationId = "agent:$nextJobId",
            )
            val nextSchedule = jobLedger.advanceSchedule(
                profileId = schedule.profileId,
                scheduleId = schedule.scheduleId,
                expectedUpdatedAt = schedule.updatedAt,
                expectedJobId = schedule.jobId,
                nextJobId = nextJobId,
                nextRunAt = System.currentTimeMillis() + interval,
                enabled = true,
            )
            if (nextSchedule != null) {
                AgentJobNotification.show(appContext!!, next)
                publishAgentSummary()
                scheduleAlarm(nextSchedule)
            }
        } else {
            jobLedger.setScheduleDue(schedule.profileId, schedule.scheduleId, schedule.updatedAt, ALARM_MAX, enabled = false)
        }
        return succeeded
    }

    private fun finishPaused(current: JobRecord, streamId: String?, payload: AgentJobPayload): JobRecord? {
        runCatching { AgentJobClient { runtime }.cancel(streamId) }
        val paused = ledger?.transition(
            profileId = current.profileId,
            jobId = current.jobId,
            expected = current.state,
            expectedUpdatedAt = current.updatedAt,
            next = JobState.PAUSED,
            error = "worker paused or runtime unavailable",
            workerId = workerId,
        ) ?: return null
        AgentJobNotification.show(appContext!!, paused)
        publishAgentSummary()
        ledger?.scheduleForJob(current.profileId, current.jobId)?.let {
            ledger?.setScheduleDue(current.profileId, it.scheduleId, it.updatedAt, ALARM_MAX, enabled = false)
        }
        return paused
    }

    private fun finishCancelled(current: JobRecord, streamId: String?, payload: AgentJobPayload): JobRecord? {
        runCatching { AgentJobClient { runtime }.cancel(streamId) }
        val cancelled = ledger?.transition(
            profileId = current.profileId,
            jobId = current.jobId,
            expected = current.state,
            expectedUpdatedAt = current.updatedAt,
            next = JobState.CANCELLED,
            workerId = workerId,
        ) ?: return null
        AgentJobNotification.show(appContext!!, cancelled)
        publishAgentSummary()
        ledger?.scheduleForJob(current.profileId, current.jobId)?.let {
            ledger?.setScheduleDue(current.profileId, it.scheduleId, it.updatedAt, ALARM_MAX, enabled = false)
        }
        return cancelled
    }

    private fun finishFailed(current: JobRecord, message: String): JobRecord? {
        if (current.state == JobState.QUEUED) return finishFailedQueued(current, message)
        val failed = ledger?.transition(
            profileId = current.profileId,
            jobId = current.jobId,
            expected = current.state,
            expectedUpdatedAt = current.updatedAt,
            next = JobState.FAILED,
            error = message,
            workerId = workerId,
        ) ?: return null
        AgentJobNotification.show(appContext!!, failed)
        publishAgentSummary()
        ledger?.scheduleForJob(current.profileId, current.jobId)?.let {
            ledger?.setScheduleDue(current.profileId, it.scheduleId, it.updatedAt, ALARM_MAX, enabled = false)
        }
        return failed
    }

    private fun finishFailedQueued(current: JobRecord, message: String): JobRecord? {
        val schedule = ledger?.scheduleForJob(current.profileId, current.jobId)
        val failed = ledger?.transition(
            profileId = current.profileId,
            jobId = current.jobId,
            expected = JobState.QUEUED,
            expectedUpdatedAt = current.updatedAt,
            next = JobState.FAILED,
            error = message,
        ) ?: return null
        schedule?.let {
            ledger?.setScheduleDue(current.profileId, it.scheduleId, it.updatedAt, ALARM_MAX, enabled = false)
        }
        AgentJobNotification.show(appContext!!, failed)
        publishAgentSummary()
        return failed
    }

    private fun scheduleAlarm(schedule: JobSchedule) {
        val context = appContext ?: return
        if (!schedule.enabled || schedule.nextRunAt == ALARM_MAX) return
        if (schedule.nextRunAt <= System.currentTimeMillis()) return
        val manager = context.getSystemService(Context.ALARM_SERVICE) as AlarmManager
        val intent = Intent(context, AgentJobAlarmReceiver::class.java)
            .setAction(AgentJobAlarmReceiver.ACTION)
            .putExtra(AgentJobAlarmReceiver.EXTRA_JOB_ID, schedule.jobId)
            .putExtra(AgentJobAlarmReceiver.EXTRA_PROFILE, schedule.profileId)
        val pending = PendingIntent.getBroadcast(
            context,
            alarmRequestCode(schedule.jobId),
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or pendingFlags(),
        )
        val at = maxOf(System.currentTimeMillis(), schedule.nextRunAt)
        if (Build.VERSION.SDK_INT >= 23) manager.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, at, pending)
        else manager.set(AlarmManager.RTC_WAKEUP, at, pending)
    }

    private fun cancelAlarm(jobId: String) {
        val context = appContext ?: return
        val intent = Intent(context, AgentJobAlarmReceiver::class.java).setAction(AgentJobAlarmReceiver.ACTION)
        val pending = PendingIntent.getBroadcast(
            context,
            alarmRequestCode(jobId),
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or pendingFlags(),
        )
        (context.getSystemService(Context.ALARM_SERVICE) as AlarmManager).cancel(pending)
    }

    private fun rescheduleAlarms() {
        val now = System.currentTimeMillis()
        ledger?.list(AGENT_PROFILE, 200)?.forEach { record ->
            ledger?.scheduleForJob(AGENT_PROFILE, record.jobId)?.let { schedule ->
                if (!schedule.enabled || schedule.nextRunAt <= now) return@let
                scheduleAlarm(schedule)
            }
        }
    }

    private fun wakeService() {
        val context = appContext ?: return
        HermesService.send(context, HermesService.ACTION_RUN_JOBS)
    }

    private fun jobJson(record: JobRecord, schedule: JobSchedule?): JSONObject = JSONObject().apply {
        put("jobId", record.jobId)
        put("profileId", record.profileId)
        put("kind", "agent")
        put("state", record.state.value)
        put("createdAt", record.createdAt)
        put("updatedAt", record.updatedAt)
        put("startedAt", record.startedAt ?: JSONObject.NULL)
        put("finishedAt", record.finishedAt ?: JSONObject.NULL)
        put("retryCount", record.retryCount)
        put("lastError", record.lastError ?: JSONObject.NULL)
        put("sessionId", record.sessionId ?: JSONObject.NULL)
        put("workerId", record.workerId ?: JSONObject.NULL)
        put("heartbeatAt", record.heartbeatAt ?: JSONObject.NULL)
        put("nextRunAt", schedule?.nextRunAt ?: JSONObject.NULL)
        put("intervalMs", schedule?.intervalMs ?: JSONObject.NULL)
        put("scheduleEnabled", schedule?.enabled ?: false)
    }

    private fun updatedJob(record: JobRecord, notificationId: String?): JobRecord =
        record.copy(notificationId = notificationId)

    private fun publishAgentSummary() {
        val jobLedger = ledger ?: return
        val active = jobLedger.list(AGENT_PROFILE, 200).count { !it.state.isTerminal }
        HermesRuntime.update { it.copy(agentJobs = active) }
    }

    private fun alarmRequestCode(jobId: String): Int = 2_000_000 + (jobId.hashCode() and 0x7fffffff) % 100_000

    private fun pendingFlags(): Int = if (Build.VERSION.SDK_INT >= 23) PendingIntent.FLAG_IMMUTABLE else 0

    private enum class JobAction { PAUSE, CANCEL }
}
