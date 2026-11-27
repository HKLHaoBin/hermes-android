package io.github.hermesandroid

import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStream
import java.io.InputStreamReader
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder
import java.nio.charset.StandardCharsets
import java.util.regex.Pattern

/** Small authenticated HTTP client for the local Hermes WebUI Agent endpoints. */
class AgentJobClient(private val runtime: () -> HermesRuntime.Snapshot) {
    data class SessionStatus(
        val running: Boolean,
        val streamId: String?,
        val error: String? = null,
        val workspace: String? = null,
        val model: String? = null,
        val modelProvider: String? = null,
        val profile: String? = null,
    )

    fun start(payload: AgentJobPayload): String? {
        val session = sessionStatus(payload.sessionId)
        val body = JSONObject().apply {
            put("session_id", payload.sessionId)
            put("message", payload.prompt)
            put("workspace", session.workspace ?: JSONObject.NULL)
            put("model", session.model ?: JSONObject.NULL)
            put("model_provider", session.modelProvider ?: JSONObject.NULL)
            put("profile", session.profile ?: payload.profileId)
            put("goal_continuation_id", "")
        }
        val result = request("POST", "/api/chat/start", body, csrf = true)
        val nested = result.optJSONObject("stream")
        return result.optString("stream_id")
            .ifBlank { result.optString("id") }
            .ifBlank { nested?.optString("id").orEmpty() }
            .ifBlank { null }
    }

    fun sessionStatus(sessionId: String): SessionStatus {
        val encoded = URLEncoder.encode(sessionId, StandardCharsets.UTF_8.name())
        val result = request("GET", "/api/session?session_id=$encoded&messages=0")
        val session = result.optJSONObject("session") ?: result
        val streamId = session.optString("active_stream_id")
            .ifBlank { session.optString("active_stream") }
            .ifBlank { session.optString("stream_id") }
            .ifBlank { null }
        val running = if (session.has("running")) {
            session.optBoolean("running", false)
        } else {
            streamId != null || !session.isNull("pending_user_message") || session.optBoolean("busy", false)
        }
        return SessionStatus(
            running = running,
            streamId = streamId,
            workspace = session.optString("workspace").ifBlank { null },
            model = session.optString("model").ifBlank { null },
            modelProvider = session.optString("model_provider").ifBlank { session.optString("provider").ifBlank { null } },
            profile = session.optString("profile").ifBlank { null },
        )
    }

    fun cancel(streamId: String?) {
        if (streamId.isNullOrBlank()) return
        val encoded = URLEncoder.encode(streamId, StandardCharsets.UTF_8.name())
        request("GET", "/api/chat/cancel?stream_id=$encoded")
    }

    private fun request(method: String, path: String, body: JSONObject? = null, csrf: Boolean = false): JSONObject {
        val snapshot = runtime()
        require(snapshot.phase == HermesRuntime.Phase.RUNNING) { "Hermes runtime is not running" }
        require(snapshot.port > 0 && snapshot.webCookie.isNotBlank()) { "Hermes runtime session is unavailable" }
        val base = "http://127.0.0.1:${snapshot.port}"
        val connection = (URL(base + path).openConnection() as HttpURLConnection).apply {
            requestMethod = method
            connectTimeout = 5_000
            readTimeout = 20_000
            instanceFollowRedirects = false
            setRequestProperty("Accept", "application/json")
            setRequestProperty("Cookie", snapshot.webCookie)
            setRequestProperty("Origin", base)
            setRequestProperty("Referer", "$base/")
            if (snapshot.token.isNotBlank()) setRequestProperty("X-Hermes-Session-Token", snapshot.token)
        }
        try {
            if (csrf) connection.setRequestProperty("X-Hermes-CSRF-Token", csrfToken(snapshot))
            if (body != null) {
                connection.doOutput = true
                connection.setRequestProperty("Content-Type", "application/json; charset=utf-8")
                connection.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }
            }
            val code = connection.responseCode
            val text = readLimited(if (code >= 400) connection.errorStream else connection.inputStream)
            if (code !in 200..299) throw IllegalStateException("Hermes Agent HTTP $code")
            return if (text.isBlank()) JSONObject() else JSONObject(text)
        } finally {
            connection.disconnect()
        }
    }

    private fun csrfToken(snapshot: HermesRuntime.Snapshot): String {
        val base = "http://127.0.0.1:${snapshot.port}"
        val connection = (URL("$base/").openConnection() as HttpURLConnection).apply {
            requestMethod = "GET"
            connectTimeout = 5_000
            readTimeout = 10_000
            setRequestProperty("Accept", "text/html")
            setRequestProperty("Cookie", snapshot.webCookie)
            setRequestProperty("Origin", base)
            setRequestProperty("Referer", "$base/")
        }
        return try {
            val html = readLimited(connection.inputStream)
            CSRF_PATTERN.matcher(html).takeIf { it.find() }?.group(1).orEmpty()
        } finally {
            connection.disconnect()
        }
    }

    private fun readLimited(stream: InputStream?): String {
        if (stream == null) return ""
        val output = StringBuilder()
        BufferedReader(InputStreamReader(stream, Charsets.UTF_8)).use { reader ->
            val buffer = CharArray(4096)
            while (output.length < MAX_RESPONSE_CHARS) {
                val read = reader.read(buffer)
                if (read <= 0) break
                output.append(buffer, 0, minOf(read, MAX_RESPONSE_CHARS - output.length))
            }
        }
        return output.toString()
    }

    companion object {
        private const val MAX_RESPONSE_CHARS = 2 * 1024 * 1024
        private val CSRF_PATTERN = Pattern.compile("csrfToken\\s*:\\s*[\\\"']([^\\\"']+)[\\\"']")
    }
}
