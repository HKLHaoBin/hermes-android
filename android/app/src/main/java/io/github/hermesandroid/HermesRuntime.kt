package io.github.hermesandroid

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.CopyOnWriteArraySet

/** Process-wide state of the on-device Hermes runtime, shared by the service and the UI. */
object HermesRuntime {

    enum class Phase { IDLE, INSTALLING, STARTING, RUNNING, STOPPING, STOPPED, ERROR }

    data class Snapshot(
        val phase: Phase = Phase.IDLE,
        val message: String = "",
        /** 0..1 while INSTALLING, otherwise -1. */
        val progress: Float = -1f,
        val port: Int = 0,
        val token: String = "",
        val webCookie: String = "",
        val pid: Int = 0,
        val startedAt: Long = 0L,
        val payloadId: String = "",
        val exitCode: Int? = null,
    )

    @Volatile
    var snapshot = Snapshot()
        private set

    private val listeners = CopyOnWriteArraySet<(Snapshot) -> Unit>()

    fun update(transform: (Snapshot) -> Snapshot) {
        val next: Snapshot
        synchronized(this) {
            next = transform(snapshot)
            snapshot = next
        }
        for (listener in listeners) {
            try {
                listener(next)
            } catch (_: Throwable) {
            }
        }
    }

    fun addListener(listener: (Snapshot) -> Unit) {
        listeners.add(listener)
    }

    fun removeListener(listener: (Snapshot) -> Unit) {
        listeners.remove(listener)
    }

    // ── recent log lines (the full log is written to files/logs/hermes.log) ──

    private const val MAX_LINES = 2000
    private val lines = ArrayDeque<String>()

    fun appendLog(line: String) {
        android.util.Log.i("Hermes", line)
        synchronized(lines) {
            lines.addLast(line)
            while (lines.size > MAX_LINES) lines.removeFirst()
        }
    }

    fun recentLog(max: Int): List<String> = synchronized(lines) {
        val from = (lines.size - max).coerceAtLeast(0)
        lines.toList().subList(from, lines.size)
    }

    /** Status for the web UI. The session token is included only for same-origin callers. */
    fun statusJson(context: Context, includeToken: Boolean): JSONObject {
        val s = snapshot
        return JSONObject().apply {
            put("phase", s.phase.name.lowercase())
            put("message", s.message)
            put("progress", s.progress.toDouble())
            put("port", s.port)
            put("pid", s.pid)
            put("startedAt", s.startedAt)
            put("payloadId", s.payloadId)
            put("exitCode", s.exitCode ?: JSONObject.NULL)
            put("appVersion", BuildConfig.VERSION_NAME)
            put("androidApi", android.os.Build.VERSION.SDK_INT)
            put("abi", android.os.Build.SUPPORTED_ABIS.firstOrNull() ?: "unknown")
            put("schemaVersion", 1)
            put("capabilitySchemaVersion", 1)
            val toolchain = ToolchainManager.inventoryJson(context)
            put("toolchain", toolchain)
            put("capabilities", capabilitiesJson(toolchain))
            if (includeToken) put("token", s.token)
        }
    }

    /** Conservative capability declaration; tool/package detail lives in toolchain. */
    private fun capabilitiesJson(toolchain: JSONObject): JSONArray = JSONArray().apply {
        val toolchainState = toolchain.optString("state", "unavailable")
        capability(this, "webui", "supported", "本地 WebView 与 Hermes WebUI")
        capability(this, "runtime", "supported", "固定版本的 Android ARM64 runtime")
        capability(this, "cli_tui", "partial", "当前 Android 启动路径主要提供 WebUI")
        capability(this, "terminal", "partial", "Android /system/bin/sh 与 Toybox；工具链由独立目录管理")
        capability(this, "skills", "partial", "运行时可携带技能，但没有完整 Android 管理界面")
        capability(this, "plugins", "unavailable", "尚无 Android 插件安装与生命周期管理")
        capability(this, "mcp", "unavailable", "尚无 Android MCP 安装与依赖检查流程")
        capability(this, "browser", "unavailable", "未捆绑 Chromium/Electron 浏览器后端")
        capability(this, "computer_use", "unavailable", "未提供 Android computer-use 后端")
        capability(this, "background_service", "partial", "前台服务可管理 runtime，但仍受 Android/OEM 后台策略影响")
        capability(this, "job_queue", "unavailable", "尚无持久化任务队列和进程重建恢复")
        capability(this, "backup_restore", "unavailable", "尚无版本化选择性导出与恢复格式")
        capability(this, "real_device_validation", "unavailable", "容器 smoke test 不等于 Android 真机验收")
        capability(this, "toolchain", if (toolchainState == "ready") "supported" else "unavailable",
            if (toolchainState == "ready") "已加载签名 Termux ARM64 工具目录" else toolchain.optString("reason", "工具目录不可用"))
    }

    private fun capability(out: JSONArray, id: String, state: String, reason: String) {
        out.put(JSONObject().apply {
            put("id", id)
            put("state", state)
            put("reason", reason)
        })
    }

    fun logJson(max: Int): JSONObject = JSONObject().put("lines", JSONArray(recentLog(max)))
}
