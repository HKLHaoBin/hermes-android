package io.github.hermesandroid

import android.content.Context
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.util.regex.Pattern

/** App-private payload storage for Agent jobs; the JobLedger stores metadata only. */
class AgentJobPayloadStore(context: Context) {
    private val root = File(context.applicationContext.filesDir, "agent-jobs")

    fun save(payloadRef: String, payload: AgentJobPayload) {
        requireValidRef(payloadRef)
        require(payload.profileId.isNotBlank()) { "profileId must not be blank" }
        require(payload.sessionId.isNotBlank()) { "sessionId must not be blank" }
        require(payload.prompt.isNotBlank()) { "prompt must not be blank" }
        root.mkdirs()
        val target = file(payloadRef)
        val temporary = File(root, ".${target.name}.tmp")
        val body = JSONObject().apply {
            put("profileId", payload.profileId)
            put("sessionId", payload.sessionId)
            put("prompt", payload.prompt)
            put("mode", payload.mode.value)
            put("createdAt", payload.createdAt)
        }.toString()
        FileOutputStream(temporary).use { output ->
            output.write(body.toByteArray(Charsets.UTF_8))
            output.fd.sync()
        }
        check(temporary.renameTo(target)) { "failed to publish Agent job payload" }
    }

    fun load(payloadRef: String): AgentJobPayload? {
        requireValidRef(payloadRef)
        val target = file(payloadRef)
        if (!target.isFile) return null
        val json = JSONObject(target.readText(Charsets.UTF_8))
        return AgentJobPayload(
            profileId = json.getString("profileId"),
            sessionId = json.getString("sessionId"),
            prompt = json.getString("prompt"),
            mode = AgentJobMode.fromValue(json.optString("mode", AgentJobMode.NATIVE.value)),
            createdAt = json.optLong("createdAt", 0L),
        )
    }

    fun exists(payloadRef: String): Boolean {
        requireValidRef(payloadRef)
        return file(payloadRef).isFile
    }

    private fun file(payloadRef: String): File = File(root, "$payloadRef.json")

    private fun requireValidRef(payloadRef: String) {
        require(REF_PATTERN.matcher(payloadRef).matches()) { "invalid payloadRef" }
    }

    companion object {
        private val REF_PATTERN = Pattern.compile("[A-Za-z0-9._-]{1,128}")
    }
}

enum class AgentJobMode(val value: String) {
    UI_HANDOFF("ui"),
    NATIVE("native"),
    ;

    companion object {
        fun fromValue(value: String): AgentJobMode = entries.firstOrNull { it.value == value }
            ?: error("unknown Agent job mode: $value")
    }
}

data class AgentJobPayload(
    val profileId: String,
    val sessionId: String,
    val prompt: String,
    val mode: AgentJobMode,
    val createdAt: Long,
)
