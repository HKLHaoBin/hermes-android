package io.github.hermesandroid

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import java.util.UUID

/**
 * Persistent, profile-scoped metadata for long-running Android jobs.
 *
 * This class deliberately stores no prompts, provider credentials, cookies, or tokens.
 * State changes use the expected previous state in the UPDATE predicate so an older
 * worker cannot overwrite a newer transition.
 */
class JobLedger(context: Context) : SQLiteOpenHelper(
    context.applicationContext,
    DATABASE_NAME,
    null,
    DATABASE_VERSION,
) {
    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL(
            """
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY NOT NULL,
                profile_id TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                started_at INTEGER,
                finished_at INTEGER,
                retry_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                runtime_id TEXT,
                session_id TEXT,
                notification_id TEXT
            )
            """.trimIndent(),
        )
        db.execSQL("CREATE INDEX jobs_profile_updated ON jobs(profile_id, updated_at DESC)")
        db.execSQL("CREATE INDEX jobs_profile_state ON jobs(profile_id, state)")
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        if (oldVersion < 1) onCreate(db)
    }

    @Synchronized
    fun create(
        profileId: String,
        jobId: String = UUID.randomUUID().toString(),
        now: Long = System.currentTimeMillis(),
        runtimeId: String? = null,
        sessionId: String? = null,
        notificationId: String? = null,
    ): JobRecord {
        require(profileId.isNotBlank()) { "profileId must not be blank" }
        require(jobId.isNotBlank()) { "jobId must not be blank" }
        val db = writableDatabase
        db.beginTransaction()
        try {
            val existing = findById(db, jobId)
            if (existing != null) {
                check(existing.profileId == profileId) { "jobId belongs to another profile" }
                db.setTransactionSuccessful()
                return existing
            }
            val values = ContentValues().apply {
                put("job_id", jobId)
                put("profile_id", profileId)
                put("state", JobState.QUEUED.value)
                put("created_at", now)
                put("updated_at", now)
                put("retry_count", 0)
                putNullable("runtime_id", runtimeId)
                putNullable("session_id", sessionId)
                putNullable("notification_id", notificationId)
            }
            val inserted = db.insertWithOnConflict(TABLE, null, values, SQLiteDatabase.CONFLICT_IGNORE)
            if (inserted == -1L) {
                val raced = findById(db, jobId)
                check(raced != null && raced.profileId == profileId) { "jobId belongs to another profile" }
                db.setTransactionSuccessful()
                return raced
            }
            db.setTransactionSuccessful()
            return find(db, profileId, jobId)!!
        } finally {
            db.endTransaction()
        }
    }

    @Synchronized
    fun get(profileId: String, jobId: String): JobRecord? =
        find(readableDatabase, profileId, jobId)

    @Synchronized
    fun list(profileId: String, limit: Int = DEFAULT_LIMIT): List<JobRecord> {
        require(profileId.isNotBlank()) { "profileId must not be blank" }
        val safeLimit = limit.coerceIn(1, MAX_LIMIT)
        val result = ArrayList<JobRecord>()
        readableDatabase.query(
            TABLE,
            COLUMNS,
            "profile_id = ?",
            arrayOf(profileId),
            null,
            null,
            "updated_at DESC",
            safeLimit.toString(),
        ).use { cursor ->
            while (cursor.moveToNext()) result += cursor.toJobRecord()
        }
        return result
    }

    /** Returns the updated row, or null when the expected state no longer matches. */
    @Synchronized
    fun transition(
        profileId: String,
        jobId: String,
        expected: JobState,
        next: JobState,
        now: Long = System.currentTimeMillis(),
        error: String? = null,
    ): JobRecord? {
        require(canTransition(expected, next)) { "illegal transition: ${expected.value} -> ${next.value}" }
        if (expected == JobState.PAUSED &&
            next in setOf(JobState.RUNNING, JobState.QUEUED, JobState.FAILED) ||
            expected == JobState.FAILED && next == JobState.QUEUED) {
            return null // Recovery must go through resume() so the retry ceiling is enforced.
        }
        val current = get(profileId, jobId) ?: return null
        if (current.state != expected) return null
        val effectiveNow = maxOf(now, current.updatedAt + 1)
        val values = ContentValues().apply {
            put("state", next.value)
            put("updated_at", effectiveNow)
            if (next == JobState.RUNNING && current.startedAt == null) put("started_at", effectiveNow)
            if (next.isTerminal) {
                require(current.startedAt == null || effectiveNow >= current.startedAt) { "finished_at precedes started_at" }
                put("finished_at", effectiveNow)
            } else putNull("finished_at")
            if (next == JobState.FAILED) putNullable("last_error", safeError(error))
            else if (next != JobState.PAUSED) putNull("last_error")
        }
        val db = writableDatabase
        db.beginTransaction()
        try {
            val changed = db.update(
                TABLE,
                values,
                "job_id = ? AND profile_id = ? AND state = ? AND updated_at = ?",
                arrayOf(jobId, profileId, expected.value, current.updatedAt.toString()),
            )
            val result = if (changed == 1) findVersion(profileId, jobId, next, effectiveNow) else null
            db.setTransactionSuccessful()
            return result
        } finally {
            db.endTransaction()
        }
    }

    /** Resume a paused/failed job into queued, incrementing retry_count once. */
    @Synchronized
    fun resume(profileId: String, jobId: String, now: Long = System.currentTimeMillis()): JobRecord? {
        val current = get(profileId, jobId) ?: return null
        require(current.state == JobState.PAUSED || current.state == JobState.FAILED) {
            "only paused or failed jobs can resume"
        }
        if (current.retryCount >= MAX_RETRIES) {
            if (current.state == JobState.FAILED) return current
            val effectiveNow = maxOf(now, current.updatedAt + 1)
            val values = ContentValues().apply {
                put("state", JobState.FAILED.value)
                put("updated_at", effectiveNow)
                putNull("finished_at")
                put("last_error", "重试次数已达上限")
            }
            val db = writableDatabase
            db.beginTransaction()
            try {
                val changed = db.update(
                    TABLE,
                    values,
                    "job_id = ? AND profile_id = ? AND state = ? AND updated_at = ?",
                    arrayOf(jobId, profileId, JobState.PAUSED.value, current.updatedAt.toString()),
                )
                val result = if (changed == 1) findVersion(profileId, jobId, JobState.FAILED, effectiveNow) else null
                db.setTransactionSuccessful()
                return result
            } finally {
                db.endTransaction()
            }
        }
        val effectiveNow = maxOf(now, current.updatedAt + 1)
        val values = ContentValues().apply {
            put("state", JobState.QUEUED.value)
            put("updated_at", effectiveNow)
            putNull("finished_at")
            put("last_error", null as String?)
            put("retry_count", current.retryCount + 1)
        }
        val db = writableDatabase
        db.beginTransaction()
        try {
            val changed = db.update(
                TABLE,
                values,
                "job_id = ? AND profile_id = ? AND state = ? AND updated_at = ?",
                arrayOf(jobId, profileId, current.state.value, current.updatedAt.toString()),
            )
            val result = if (changed == 1) findVersion(profileId, jobId, JobState.QUEUED, effectiveNow) else null
            db.setTransactionSuccessful()
            return result
        } finally {
            db.endTransaction()
        }
    }

    /** Mark running jobs not present in [liveJobIds] as paused after process recovery. */
    @Synchronized
    fun markStale(
        profileId: String,
        liveJobIds: Set<String>,
        now: Long = System.currentTimeMillis(),
    ): Int {
        require(profileId.isNotBlank()) { "profileId must not be blank" }
        val running = listByState(profileId, JobState.RUNNING)
        val db = writableDatabase
        db.beginTransaction()
        try {
            var changed = 0
            for (job in running) {
                if (job.jobId in liveJobIds) continue
                val effectiveNow = maxOf(now, job.updatedAt + 1)
                val values = ContentValues().apply {
                    put("state", JobState.PAUSED.value)
                    put("updated_at", effectiveNow)
                    put("last_error", "Android 进程恢复后未找到活动任务")
                    putNull("finished_at")
                }
                changed += db.update(
                    TABLE,
                    values,
                    "job_id = ? AND profile_id = ? AND state = ? AND updated_at = ?",
                    arrayOf(job.jobId, profileId, JobState.RUNNING.value, job.updatedAt.toString()),
                )
            }
            db.setTransactionSuccessful()
            return changed
        } finally {
            db.endTransaction()
        }
    }

    private fun listByState(profileId: String, state: JobState): List<JobRecord> {
        val result = ArrayList<JobRecord>()
        readableDatabase.query(
            TABLE,
            COLUMNS,
            "profile_id = ? AND state = ?",
            arrayOf(profileId, state.value),
            null,
            null,
            "updated_at DESC",
            null,
        ).use { cursor ->
            while (cursor.moveToNext()) result += cursor.toJobRecord()
        }
        return result
    }

    private fun findById(db: SQLiteDatabase, jobId: String): JobRecord? =
        db.query(
            TABLE,
            COLUMNS,
            "job_id = ?",
            arrayOf(jobId),
            null,
            null,
            null,
            "1",
        ).use { cursor -> if (cursor.moveToFirst()) cursor.toJobRecord() else null }

    private fun findVersion(profileId: String, jobId: String, state: JobState, updatedAt: Long): JobRecord? =
        readableDatabase.query(
            TABLE,
            COLUMNS,
            "profile_id = ? AND job_id = ? AND state = ? AND updated_at = ?",
            arrayOf(profileId, jobId, state.value, updatedAt.toString()),
            null,
            null,
            null,
            "1",
        ).use { cursor -> if (cursor.moveToFirst()) cursor.toJobRecord() else null }

    private fun find(db: SQLiteDatabase, profileId: String, jobId: String): JobRecord? =
        db.query(
            TABLE,
            COLUMNS,
            "profile_id = ? AND job_id = ?",
            arrayOf(profileId, jobId),
            null,
            null,
            null,
            "1",
        ).use { cursor -> if (cursor.moveToFirst()) cursor.toJobRecord() else null }

    private fun android.database.Cursor.toJobRecord(): JobRecord = JobRecord(
        jobId = getString(getColumnIndexOrThrow("job_id")),
        profileId = getString(getColumnIndexOrThrow("profile_id")),
        state = JobState.fromValue(getString(getColumnIndexOrThrow("state"))),
        createdAt = getLong(getColumnIndexOrThrow("created_at")),
        updatedAt = getLong(getColumnIndexOrThrow("updated_at")),
        startedAt = getNullableLong("started_at"),
        finishedAt = getNullableLong("finished_at"),
        retryCount = getInt(getColumnIndexOrThrow("retry_count")),
        lastError = getNullableString("last_error"),
        runtimeId = getNullableString("runtime_id"),
        sessionId = getNullableString("session_id"),
        notificationId = getNullableString("notification_id"),
    )

    private fun safeError(error: String?): String? {
        val text = error?.trim()?.lowercase() ?: return null
        if (text.isEmpty()) return null
        return when {
            "timeout" in text || "timed out" in text || "超时" in text -> "网络或任务超时"
            "permission" in text || "denied" in text || "权限" in text -> "权限错误"
            "network" in text || "connection" in text || "网络" in text -> "网络错误"
            "storage" in text || "disk" in text || "存储" in text -> "存储空间错误"
            else -> "任务执行失败"
        }
    }

    private fun ContentValues.putNullable(key: String, value: String?) {
        if (value == null) putNull(key) else put(key, value)
    }

    private fun android.database.Cursor.getNullableLong(name: String): Long? {
        val index = getColumnIndexOrThrow(name)
        return if (isNull(index)) null else getLong(index)
    }

    private fun android.database.Cursor.getNullableString(name: String): String? {
        val index = getColumnIndexOrThrow(name)
        return if (isNull(index)) null else getString(index)
    }

    override fun onConfigure(db: SQLiteDatabase) {
        super.onConfigure(db)
        db.setForeignKeyConstraintsEnabled(true)
    }

    companion object {
        private const val DATABASE_NAME = "hermes_jobs.db"
        private const val DATABASE_VERSION = 1
        private const val TABLE = "jobs"
        private const val DEFAULT_LIMIT = 50
        private const val MAX_LIMIT = 200
        private const val MAX_RETRIES = 3
        private val COLUMNS = arrayOf(
            "job_id", "profile_id", "state", "created_at", "updated_at", "started_at",
            "finished_at", "retry_count", "last_error", "runtime_id", "session_id",
            "notification_id",
        )

        fun canTransition(old: JobState, next: JobState): Boolean = next in when (old) {
            JobState.QUEUED -> setOf(JobState.RUNNING, JobState.CANCELLED)
            JobState.RUNNING -> setOf(JobState.PAUSED, JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED)
            JobState.PAUSED -> setOf(JobState.QUEUED, JobState.RUNNING, JobState.FAILED, JobState.CANCELLED)
            JobState.FAILED -> setOf(JobState.QUEUED, JobState.CANCELLED)
            JobState.SUCCEEDED, JobState.CANCELLED -> emptySet()
        }
    }
}

enum class JobState(val value: String, val isTerminal: Boolean = false) {
    QUEUED("queued"),
    RUNNING("running"),
    PAUSED("paused"),
    SUCCEEDED("succeeded", true),
    FAILED("failed"),
    CANCELLED("cancelled", true),
    ;

    companion object {
        fun fromValue(value: String): JobState = entries.firstOrNull { it.value == value }
            ?: error("unknown job state: $value")
    }
}

data class JobRecord(
    val jobId: String,
    val profileId: String,
    val state: JobState,
    val createdAt: Long,
    val updatedAt: Long,
    val startedAt: Long?,
    val finishedAt: Long?,
    val retryCount: Int,
    val lastError: String?,
    val runtimeId: String?,
    val sessionId: String?,
    val notificationId: String?,
)
