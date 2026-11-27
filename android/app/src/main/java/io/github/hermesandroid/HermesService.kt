package io.github.hermesandroid

import android.app.Notification
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.net.HttpURLConnection
import java.net.InetAddress
import java.net.ServerSocket
import java.net.URL
import java.security.SecureRandom
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import android.util.Base64
import kotlin.concurrent.thread

/**
 * Foreground service that owns the Hermes runtime process: unpacks the payload on
 * first run, starts `hermes dashboard` on 127.0.0.1, pumps its log, and stops the
 * whole process tree on request.
 */
class HermesService : Service() {

    private var worker: Thread? = null
    @Volatile private var process: java.lang.Process? = null
    @Volatile private var pid: Int = 0
    @Volatile private var stopping = false
    private var wakeLock: PowerManager.WakeLock? = null
    private val crashTimes = java.util.concurrent.CopyOnWriteArrayList<Long>()

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startForeground(NOTIFICATION_ID, notification())
        when (intent?.action) {
            ACTION_STOP -> {
                thread(name = "hermes-stop") {
                    stopRuntime()
                    stopForegroundCompat()
                    stopSelf()
                }
                return START_NOT_STICKY
            }
            ACTION_RESTART -> thread(name = "hermes-restart") {
                stopRuntime()
                worker?.join()
                crashTimes.clear()
                launch()
            }
            else -> launch()
        }
        return START_STICKY
    }

    override fun onDestroy() {
        HermesRuntime.removeListener(notifier)
        thread(name = "hermes-destroy") { stopRuntime() }
        releaseWakeLock()
        super.onDestroy()
    }

    private val notifier: (HermesRuntime.Snapshot) -> Unit = { updateNotification() }

    override fun onCreate() {
        super.onCreate()
        HermesRuntime.addListener(notifier)
    }

    @Synchronized
    private fun launch() {
        if (worker?.isAlive == true) return
        stopping = false
        worker = thread(name = "hermes-runtime") { runRuntime() }
    }

    private fun runRuntime() {
        val installer = PayloadInstaller(this)
        var readyFile: File? = null
        var attemptedRoot: String? = null
        var pump: Thread? = null
        try {
            attemptedRoot = installer.root.absolutePath
            if (!installer.hasPayload()) {
                throw IOException("这个安装包没有内置 Hermes 运行时（构建时缺少 payload）")
            }
            val includedManifest = installer.manifest()
            if (installer.activeSlotId() == null) quiescePayload(installer.root.absolutePath)
            installer.migrateLegacy(includedManifest)
            val recovery = installer.recoverPending(::quiescePayload)
            attemptedRoot = installer.root.absolutePath
            val manifest = installer.selectedManifest(includedManifest)
            val usingFallback = manifest.getString("payload_id") != includedManifest.getString("payload_id")
            val updateError = if (usingFallback) "payload-startup-rollback" else ""
            if (recovery.hadPendingActivation && recovery.restoredPayloadId != null) {
                log("上次运行环境未通过启动检查，改用 previous-good：${recovery.restoredPayloadId}")
            } else if (usingFallback) {
                log("跳过已拒绝的 APK payload，继续使用 previous-good")
            }
            val payloadId = manifest.getString("payload_id")
            applyManifestIdentity(installer, manifest, updateError)
            HermesRuntime.update { it.copy(exitCode = null) }

            quiescePayload(installer.root.absolutePath)
            installer.pruneStaleSlots()

            val needsInstall = installer.needsInstall(manifest)
            val canReuse = installer.canReuseSlot(manifest)
            if (needsInstall && !canReuse) {
                val payloadBytes = manifest.optLong("total_bytes", 0L).coerceAtLeast(0L)
                val need = payloadBytes * 2L + 200L * 1024 * 1024
                val free = filesDir.usableSpace
                if (free < need) {
                    fail("存储空间不足：需要约 ${need / 1_000_000} MB，当前可用 ${free / 1_000_000} MB")
                    return
                }
            }
            if (needsInstall) {
                HermesRuntime.update { it.copy(phase = HermesRuntime.Phase.INSTALLING, progress = 0f, message = "首次运行，正在解压运行环境…") }
                installer.install(manifest, { p, label ->
                    HermesRuntime.update { it.copy(progress = p, message = label) }
                }) { stopping }
                attemptedRoot = installer.root.absolutePath
                applyManifestIdentity(installer, manifest, updateError)
                log("运行环境已安装：$payloadId")
            }
            if (stopping) {
                if (installer.activationPending()) {
                    val restored = installer.rollback(::quiescePayload, rejectFailed = false)
                    applyManifestIdentity(installer, installer.installedManifest() ?: org.json.JSONObject(), "payload-install-cancelled")
                    if (restored != null) log("停止期间取消运行环境激活，恢复 previous-good：$restored")
                }
                return
            }

            try {
                RuntimeEnv.installConsoleThemes(this)
            } catch (e: Exception) {
                log("控制台主题写入失败：${e.message}")
            }
            val port = choosePort(Prefs.port(this))
            val token = newToken()
            HermesRuntime.update {
                it.copy(phase = HermesRuntime.Phase.STARTING, progress = -1f, port = port, token = "", webCookie = "",
                    message = "正在启动 Hermes…", startedAt = System.currentTimeMillis())
            }
            acquireWakeLock()

            val cmd = RuntimeEnv.command(installer.root, manifest, port)
            val pb = ProcessBuilder(cmd)
                .directory(RuntimeEnv.home(this))
                .redirectErrorStream(true)
            val env = pb.environment()
            RuntimeEnv.SCRUB.forEach { env.remove(it) }
            env.putAll(RuntimeEnv.environment(this, installer.root, manifest, token))
            val ready = File(cacheDir, "webui-$token.ready")
            readyFile = ready
            env["HERMES_ANDROID_READY"] = ready.absolutePath
            env["HERMES_WEBUI_HOST"] = "127.0.0.1"
            env["HERMES_WEBUI_PORT"] = port.toString()
            env["HERMES_WEBUI_AGENT_DIR"] = env.getValue("HERMES_PYTHON_SRC_ROOT")
            env["HERMES_WEBUI_PASSWORD"] = token
            env.remove("HERMES_DASHBOARD_SESSION_TOKEN")
            log("启动：Hermes WebUI，手机本地端口 $port")
            val proc = pb.start()
            process = proc
            pid = ProcUtil.findChild(cmd[0]) ?: 0
            HermesRuntime.update { it.copy(pid = pid) }
            pump = thread(name = "hermes-log") { pumpLog(proc) }

            if (waitHealthy(port, proc, ready)) {
                val cookie = ready.readText()
                ready.delete()
                installer.markHealthy()
                applyManifestIdentity(installer, manifest, updateError)
                HermesRuntime.update { it.copy(phase = HermesRuntime.Phase.RUNNING, webCookie = cookie, message = "运行中") }
                log("Hermes 已就绪：http://127.0.0.1:$port")
            } else {
                throw IOException("运行环境未通过启动健康检查")
            }
            val code = proc.waitFor()
            stopOwnedProcess(attemptedRoot)
            pump?.join(2000)
            if (stopping) {
                HermesRuntime.update { it.copy(phase = HermesRuntime.Phase.STOPPED, message = "已停止", exitCode = code, pid = 0) }
            } else {
                val killed = code == 137 || code == 9
                val hint = if (killed) "，可能被系统的后台限制结束" else ""
                val now = System.currentTimeMillis()
                crashTimes.removeIf { now - it > 10 * 60 * 1000L }
                crashTimes.add(now)
                if (crashTimes.size <= MAX_AUTO_RESTARTS) {
                    log("Hermes 进程退出（代码 $code$hint），自动重启（${crashTimes.size}/$MAX_AUTO_RESTARTS）")
                    HermesRuntime.update { it.copy(phase = HermesRuntime.Phase.STARTING, message = "正在自动重启…", exitCode = code, pid = 0) }
                    releaseWakeLock()
                    Thread.sleep(2000L * crashTimes.size)
                    if (!stopping) {
                        thread(name = "hermes-relaunch") {
                            worker?.join()
                            launch()
                        }
                    }
                    return
                }
                fail("Hermes 进程意外退出（代码 $code$hint），请查看日志", code)
            }
        } catch (t: Throwable) {
            log("错误：${t}")
            try {
                stopOwnedProcess(attemptedRoot)
                pump?.join(2000)
                val cancelled = stopping || t is PayloadInstaller.InstallCancelledException
                if (cancelled) {
                    if (installer.activationPending()) {
                        val restored = installer.rollback(::quiescePayload, rejectFailed = false)
                        val restoredManifest = installer.installedManifest()
                        applyManifestIdentity(installer, restoredManifest ?: org.json.JSONObject(), "payload-install-cancelled")
                        if (restored != null) log("运行环境安装已取消，恢复 previous-good：$restored")
                    } else {
                        applyManifestIdentity(installer, installer.installedManifest() ?: org.json.JSONObject(), "payload-install-cancelled")
                    }
                    return
                }
                if (installer.activationPending()) {
                    val restored = installer.rollback(::quiescePayload)
                    val restoredManifest = installer.installedManifest()
                    applyManifestIdentity(installer, restoredManifest ?: org.json.JSONObject(), "payload-activation-rollback")
                    if (!stopping && restored != null && restoredManifest != null) {
                        log("运行环境激活失败，已回滚到 $restored，重新启动 previous-good")
                        return runRuntime()
                    }
                } else {
                    applyManifestIdentity(installer, installer.installedManifest() ?: org.json.JSONObject(), "payload-activation-failed")
                }
            } catch (recoveryFailure: Throwable) {
                // Do not activate another slot while quiescence or state durability is uncertain.
                log("回滚未完成：${recoveryFailure}")
                fail("运行环境清理或回滚未完成，请查看日志", updateError = "payload-recovery-failed")
                return
            }
            fail(t.message ?: t.toString(), updateError = "payload-activation-failed")
        } finally {
            readyFile?.let { it.delete(); File(it.parentFile, "${it.nameWithoutExtension}.tmp").delete() }
            releaseWakeLock()
        }
    }

    private fun applyManifestIdentity(installer: PayloadInstaller, manifest: org.json.JSONObject, updateError: String = "") {
        val runtime = manifest.optJSONObject("runtime")
        val webui = manifest.optJSONObject("webui")
        val compatibility = manifest.optJSONObject("compatibility")
        HermesRuntime.update {
            it.copy(
                payloadId = manifest.optString("payload_id"),
                runtimeChannel = runtime?.optString("channel").orEmpty(),
                runtimeVersion = runtime?.optString("version").orEmpty(),
                runtimeCommit = runtime?.optString("source_commit").orEmpty(),
                webuiChannel = webui?.optString("channel").orEmpty(),
                webuiTag = webui?.optString("tag").orEmpty(),
                webuiCommit = webui?.optString("commit").orEmpty(),
                webuiStagedSha256 = webui?.optString("staged_sha256").orEmpty(),
                compatibilityStatus = compatibility?.optString("status", "unknown") ?: "unknown",
                activeSlot = installer.activeSlotId().orEmpty(),
                previousGoodSlot = installer.previousGoodSlotId().orEmpty(),
                activationPending = installer.activationPending(),
                updateError = updateError,
            )
        }
    }

    private fun fail(message: String, code: Int? = null, updateError: String = "") {
        HermesRuntime.update {
            it.copy(
                phase = HermesRuntime.Phase.ERROR,
                message = message,
                exitCode = code,
                pid = 0,
                progress = -1f,
                updateError = updateError.ifBlank { it.updateError },
            )
        }
    }

    /** Stop the process before changing the slot it was executing from. */
    private fun stopOwnedProcess(root: String?) {
        val proc = process
        val target = pid
        var interrupted = Thread.interrupted()
        try {
            if (target > 0) ProcUtil.stopTree(target, graceMs = 0)
            proc?.destroy()
            root?.let { quiescePayload(it) }
            while (true) {
                try {
                    proc?.waitFor()
                    break
                } catch (_: InterruptedException) {
                    interrupted = true
                }
            }
            root?.let { quiescePayload(it) }
            if (process === proc) process = null
            if (pid == target) pid = 0
        } finally {
            if (interrupted) Thread.currentThread().interrupt()
        }
    }

    /** Includes orphaned candidates after app death; failure prevents slot mutation. */
    private fun quiescePayload(root: String) {
        val owners = ProcUtil.list().filter { it.cmdline.contains(root) }
        owners.forEach { ProcUtil.stopTree(it.pid, graceMs = 0) }
        ProcUtil.sweep(root)
        val deadline = android.os.SystemClock.elapsedRealtime() + 5000L
        while (ProcUtil.list().any { it.cmdline.contains(root) }) {
            if (android.os.SystemClock.elapsedRealtime() >= deadline) throw IOException("payload process cleanup timed out")
            Thread.sleep(50)
        }
    }

    private fun waitHealthy(port: Int, proc: java.lang.Process, ready: File): Boolean {
        val deadline = System.currentTimeMillis() + 10 * 60 * 1000L
        while (System.currentTimeMillis() < deadline) {
            if (stopping || !proc.isAliveCompat()) return false
            try {
                if (!ready.isFile) { Thread.sleep(500); continue }
                val conn = URL("http://127.0.0.1:$port/health").openConnection(java.net.Proxy.NO_PROXY) as HttpURLConnection
                conn.connectTimeout = 1500
                conn.readTimeout = 3000
                conn.instanceFollowRedirects = false
                val code = conn.responseCode
                conn.disconnect()
                if (code == 200 && proc.isAliveCompat()) return true
            } catch (_: Exception) {
            }
            Thread.sleep(700)
        }
        log("等待 Hermes 就绪超时")
        return false
    }

    private fun pumpLog(proc: java.lang.Process) {
        val logFile = File(RuntimeEnv.logDir(this), "hermes.log")
        if (logFile.length() > 4L * 1024 * 1024) {
            logFile.renameTo(File(logFile.parentFile, "hermes.log.1"))
        }
        FileOutputStream(logFile, true).bufferedWriter().use { out ->
            out.write("\n===== ${stamp()} start =====\n")
            try {
                proc.inputStream.bufferedReader().forEachLine { line ->
                    HermesRuntime.appendLog(line)
                    out.write(line)
                    out.write("\n")
                    out.flush()
                }
            } catch (_: Exception) {
            }
        }
    }

    private fun stopRuntime() {
        stopping = true
        val proc = process
        if (proc != null || pid > 0) {
            HermesRuntime.update { it.copy(phase = HermesRuntime.Phase.STOPPING, message = "正在停止…") }
            val target = if (pid > 0) pid else 0
            if (target > 0) ProcUtil.stopTree(target) else proc?.destroy()
            try {
                proc?.waitFor()
            } catch (_: InterruptedException) {
            }
        }
        worker?.join()
        process = null
        pid = 0
        HermesRuntime.update { it.copy(phase = HermesRuntime.Phase.STOPPED, message = "已停止", pid = 0) }
    }

    private fun choosePort(preferred: Int): Int {
        for (candidate in listOf(preferred) + (9120..9199)) {
            try {
                ServerSocket(candidate, 1, InetAddress.getByName("127.0.0.1")).close()
                if (candidate != preferred) Prefs.setPort(this, candidate)
                return candidate
            } catch (_: Exception) {
            }
        }
        throw IllegalStateException("找不到可用的本地端口")
    }

    private fun newToken(): String {
        val bytes = ByteArray(32)
        SecureRandom().nextBytes(bytes)
        return Base64.encodeToString(bytes, Base64.URL_SAFE or Base64.NO_WRAP or Base64.NO_PADDING)
    }

    private fun log(line: String) {
        HermesRuntime.appendLog("[app] $line")
    }

    private fun stamp() = SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US).format(Date())

    // ── wake lock ──

    private fun acquireWakeLock() {
        if (!Prefs.keepAwake(this)) return
        if (wakeLock?.isHeld == true) return
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "hermes:runtime").apply {
            setReferenceCounted(false)
            acquire()
        }
    }

    private fun releaseWakeLock() {
        try {
            wakeLock?.takeIf { it.isHeld }?.release()
        } catch (_: Exception) {
        }
        wakeLock = null
    }

    // ── notification ──

    private fun notification(): Notification {
        val s = HermesRuntime.snapshot
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_UPDATE_CURRENT or pendingFlags(),
        )
        val stop = PendingIntent.getService(
            this, 1, Intent(this, HermesService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_UPDATE_CURRENT or pendingFlags(),
        )
        val (title, text) = when (s.phase) {
            HermesRuntime.Phase.INSTALLING -> getString(R.string.notif_installing) to
                (if (s.progress >= 0) "${(s.progress * 100).toInt()}%" else "")
            HermesRuntime.Phase.RUNNING -> getString(R.string.notif_running) to getString(R.string.notif_running_text)
            HermesRuntime.Phase.ERROR -> getString(R.string.notif_error) to s.message
            else -> getString(R.string.notif_starting) to s.message
        }
        val builder = if (Build.VERSION.SDK_INT >= 26) {
            Notification.Builder(this, HermesApp.CHANNEL_ID)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this).setPriority(Notification.PRIORITY_LOW)
        }
        builder.setSmallIcon(R.drawable.ic_stat_hermes)
            .setContentTitle(title)
            .setContentText(text)
            .setContentIntent(open)
            .setOngoing(s.phase != HermesRuntime.Phase.ERROR)
            .setOnlyAlertOnce(true)
            .addAction(
                Notification.Action.Builder(
                    android.graphics.drawable.Icon.createWithResource(this, R.drawable.ic_stat_hermes),
                    getString(R.string.action_stop),
                    stop,
                ).build(),
            )
        if (s.phase == HermesRuntime.Phase.INSTALLING && s.progress >= 0) {
            builder.setProgress(100, (s.progress * 100).toInt(), false)
        }
        return builder.build()
    }

    private var lastNotified = ""

    private fun updateNotification() {
        val s = HermesRuntime.snapshot
        val key = "${s.phase}|${(s.progress * 50).toInt()}|${s.message}"
        if (key == lastNotified) return
        lastNotified = key
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as android.app.NotificationManager
        nm.notify(NOTIFICATION_ID, notification())
    }

    private fun stopForegroundCompat() {
        if (Build.VERSION.SDK_INT >= 24) {
            stopForeground(Service.STOP_FOREGROUND_REMOVE)
        } else {
            @Suppress("DEPRECATION")
            stopForeground(true)
        }
    }

    private fun pendingFlags(): Int = if (Build.VERSION.SDK_INT >= 23) PendingIntent.FLAG_IMMUTABLE else 0

    companion object {
        const val ACTION_START = "io.github.hermesandroid.START"
        const val ACTION_STOP = "io.github.hermesandroid.STOP"
        const val ACTION_RESTART = "io.github.hermesandroid.RESTART"
        private const val NOTIFICATION_ID = 42
        private const val MAX_AUTO_RESTARTS = 3

        fun send(context: Context, action: String) {
            val intent = Intent(context, HermesService::class.java).setAction(action)
            if (Build.VERSION.SDK_INT >= 26) context.startForegroundService(intent) else context.startService(intent)
        }
    }
}

private fun java.lang.Process.isAliveCompat(): Boolean = try {
    exitValue()
    false
} catch (_: IllegalThreadStateException) {
    true
}
