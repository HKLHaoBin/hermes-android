package io.github.hermesandroid

import android.content.Context

/** Ledger adapter for the long-lived Hermes runtime itself, separate from Agent jobs. */
class RuntimeJobLedger(context: Context) {
    private val ledger = JobLedger(context.applicationContext)
    private var current: JobRecord? = null

    fun recover(): Int = ledger.markStale(PROFILE, emptySet())

    fun begin(now: Long = System.currentTimeMillis()): JobRecord {
        val record = ledger.create(profileId = PROFILE, now = now)
        current = record
        return record
    }

    fun markRunning(now: Long = System.currentTimeMillis()): JobRecord? {
        val record = current ?: return null
        val updated = ledger.transition(
            profileId = PROFILE,
            jobId = record.jobId,
            expected = record.state,
            expectedUpdatedAt = record.updatedAt,
            next = JobState.RUNNING,
            now = now,
        )
        current = updated ?: current
        return updated
    }

    fun attachRuntimeId(runtimeId: String, now: Long = System.currentTimeMillis()): JobRecord? {
        val record = current ?: return null
        val updated = ledger.setRuntimeId(PROFILE, record.jobId, record.updatedAt, runtimeId, now)
        current = updated ?: current
        return updated
    }

    fun markFailed(message: String, now: Long = System.currentTimeMillis()): JobRecord? {
        val record = current ?: return null
        val updated = if (record.state == JobState.RUNNING || record.state == JobState.QUEUED) {
            ledger.transition(
                profileId = PROFILE,
                jobId = record.jobId,
                expected = record.state,
                expectedUpdatedAt = record.updatedAt,
                next = JobState.FAILED,
                now = now,
                error = message,
            )
        } else null
        current = updated ?: current
        return updated
    }

    fun cancel(now: Long = System.currentTimeMillis()): JobRecord? {
        val record = current ?: return null
        val updated = when (record.state) {
            JobState.QUEUED, JobState.RUNNING -> ledger.transition(
                profileId = PROFILE,
                jobId = record.jobId,
                expected = record.state,
                expectedUpdatedAt = record.updatedAt,
                next = JobState.CANCELLED,
                now = now,
            )
            else -> record
        }
        current = updated ?: current
        return updated
    }

    fun current(): JobRecord? = current

    companion object {
        const val PROFILE = "android-runtime"
    }
}
