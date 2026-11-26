package io.github.hermesandroid

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.ActivityNotFoundException
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.CancellationSignal
import android.os.Handler
import android.os.Looper
import android.os.Process
import android.content.pm.PackageManager
import android.webkit.MimeTypeMap
import java.util.concurrent.Executors
import android.os.PowerManager
import android.provider.Settings
import android.util.Log
import android.webkit.ConsoleMessage
import android.webkit.CookieManager
import android.webkit.JavascriptInterface
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast

class MainActivity : Activity() {

    private lateinit var web: WebView
    @Volatile private var loadedPort = 0
    private var loadedStart = 0L
    private lateinit var statusLabel: android.widget.TextView

    private class PendingFileChooser(
        val callback: ValueCallback<Array<Uri>>,
        val port: Int,
        val startedAt: Long,
        val multiple: Boolean,
    ) {
        val cancellation = CancellationSignal()
        var timeout: Runnable? = null
    }

    private var fileChooser: PendingFileChooser? = null
    private var pickerLaunched = false
    private var authenticatedPageReady = false
    private var cookieInstalled = false
    private val mainHandler = Handler(Looper.getMainLooper())
    private val fileReader = Executors.newSingleThreadExecutor()
    @Volatile private var sharedText: String = ""
    private var pendingAgentJobId: String? = null

    private val runtimeListener: (HermesRuntime.Snapshot) -> Unit = { s ->
        runOnUiThread {
            if (!isFinishing && !isDestroyed) renderRuntime(HermesRuntime.snapshot)
        }
    }

    @SuppressLint("SetJavaScriptEnabled", "JavascriptInterface")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        AgentJobCoordinator.initialize(this)
        WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG)

        web = WebView(this)
        web.setBackgroundColor(Color.parseColor("#F5F4EF"))
        statusLabel = android.widget.TextView(this).apply {
            gravity = android.view.Gravity.CENTER
            textSize = 16f
            setPadding(24, 24, 24, 24)
        }
        setContentView(android.widget.FrameLayout(this).apply {
            addView(web, -1, -1)
            addView(statusLabel, -1, -1)
        })
        requestNotificationPermission()

        with(web.settings) {
            javaScriptEnabled = true
            domStorageEnabled = true
            databaseEnabled = true
            mediaPlaybackRequiresUserGesture = false
            allowFileAccess = false
            allowContentAccess = true
            loadWithOverviewMode = true
            useWideViewPort = true
            builtInZoomControls = false
            textZoom = 100
            cacheMode = WebSettings.LOAD_DEFAULT
            userAgentString = "$userAgentString HermesAndroid/${BuildConfig.VERSION_NAME}"
        }

        web.webViewClient = object : WebViewClient() {
            override fun onPageStarted(view: WebView, url: String?, favicon: android.graphics.Bitmap?) {
                authenticatedPageReady = false
                finishFileChooser(null)
            }

            override fun onPageFinished(view: WebView, url: String?) {
                authenticatedPageReady = cookieInstalled && statusLabel.visibility == android.view.View.GONE &&
                    AssetServer.isLocal(url?.let(Uri::parse), loadedPort) &&
                    AssetServer.isLocal(view.url?.let(Uri::parse), loadedPort)
                if (authenticatedPageReady) pendingAgentJobId?.let { jobId ->
                    pendingAgentJobId = null
                    view.postDelayed({ dispatchAgentJob(jobId) }, 100L)
                }
            }

            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val url = request.url
                if (AssetServer.isLocal(url, loadedPort)) return false
                openExternal(url)
                return true
            }
        }

        web.webChromeClient = object : WebChromeClient() {
            override fun onShowFileChooser(
                view: WebView,
                callback: ValueCallback<Array<Uri>>,
                params: FileChooserParams,
            ): Boolean {
                // Return true even on rejection: we own and complete this callback once.
                if (view !== web || !canChooseFiles() || fileChooser != null || pickerLaunched) {
                    callback.onReceiveValue(null)
                    fileChooserError("请等待 Hermes 本地页面就绪，或先完成当前文件选择")
                    return true
                }
                if (params.mode != FileChooserParams.MODE_OPEN &&
                    params.mode != FileChooserParams.MODE_OPEN_MULTIPLE) {
                    callback.onReceiveValue(null)
                    fileChooserError("此文件选择模式暂不支持")
                    return true
                }
                val pending = PendingFileChooser(callback, loadedPort, loadedStart,
                    params.mode == FileChooserParams.MODE_OPEN_MULTIPLE)
                fileChooser = pending
                try {
                    // Preserve WebView's media/capture route instead of forcing it through SAF.
                    val chooserIntent = if (params.isCaptureEnabled) params.createIntent() else {
                        val types = FileChooserMimeTypes.normalize(params.acceptTypes) {
                            MimeTypeMap.getSingleton().getMimeTypeFromExtension(it)
                        }
                        Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
                            addCategory(Intent.CATEGORY_OPENABLE)
                            type = if (types.size == 1) types[0] else "*/*"
                            if (types.size > 1) putExtra(Intent.EXTRA_MIME_TYPES, types)
                        }
                    }
                    chooserIntent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                    chooserIntent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, pending.multiple)
                    try {
                        startActivityForResult(chooserIntent, REQUEST_FILES)
                    } catch (missing: ActivityNotFoundException) {
                        if (chooserIntent.action != Intent.ACTION_OPEN_DOCUMENT) throw missing
                        // Some vendor ROMs omit DocumentsUI but still provide GET_CONTENT.
                        chooserIntent.action = Intent.ACTION_GET_CONTENT
                        startActivityForResult(chooserIntent, REQUEST_FILES)
                    }
                    pickerLaunched = true
                } catch (_: RuntimeException) {
                    finishFileChooser(null)
                    fileChooserError("无法打开系统文件选择器，请检查是否已启用“文件”应用")
                }
                return true
            }

            override fun onConsoleMessage(message: ConsoleMessage): Boolean {
                Log.d("HermesWeb", "${message.sourceId()}:${message.lineNumber()} ${message.message()}")
                return true
            }
        }

        web.setDownloadListener { url, _, _, _, _ -> openExternal(Uri.parse(url)) }


        HermesRuntime.addListener(runtimeListener)
        if (Prefs.autoStart(this)) {
            HermesService.send(this, HermesService.ACTION_START)
        }
        handleIntent(intent)
        renderRuntime(HermesRuntime.snapshot)
    }

    override fun onStart() {
        super.onStart()
        HermesRuntime.update { it.copy(appVisible = true) }
    }

    override fun onStop() {
        HermesRuntime.update { it.copy(appVisible = false) }
        super.onStop()
    }

    private fun renderRuntime(s: HermesRuntime.Snapshot) {
        if (s.port != loadedPort || s.startedAt != loadedStart ||
            s.phase != HermesRuntime.Phase.RUNNING || s.webCookie.isEmpty()) cookieInstalled = false
        if (s.phase != HermesRuntime.Phase.RUNNING || s.webCookie.isEmpty()) {
            authenticatedPageReady = false
            finishFileChooser(null)
            if (loadedPort != 0) web.loadUrl("about:blank")
            loadedPort = 0
            loadedStart = 0L
            statusLabel.text = s.message.ifBlank { "正在准备手机端 Hermes…" }
            statusLabel.visibility = android.view.View.VISIBLE
            web.visibility = android.view.View.INVISIBLE
            return
        }
        if (loadedPort == s.port && loadedStart == s.startedAt) return
        authenticatedPageReady = false
        finishFileChooser(null)
        loadedPort = s.port
        loadedStart = s.startedAt
        val origin = "http://127.0.0.1:${s.port}/"
        CookieManager.getInstance().setCookie(origin, s.webCookie) { accepted ->
            if (isDestroyed || loadedStart != s.startedAt || loadedPort != s.port) return@setCookie
            if (!accepted) {
                statusLabel.text = "无法建立 Hermes 本地会话，请重开应用。"
                return@setCookie
            }
            cookieInstalled = true
            statusLabel.visibility = android.view.View.GONE
            web.visibility = android.view.View.VISIBLE

            web.loadUrl(origin)
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleIntent(intent)
    }

    private fun handleIntent(intent: Intent?) {
        intent?.getStringExtra(EXTRA_JOB_ID)?.takeIf { it.isNotBlank() }?.let { jobId ->
            pendingAgentJobId = jobId
            if (authenticatedPageReady) dispatchAgentJob(jobId)
        }
        if (intent?.action == Intent.ACTION_SEND) {
            val text = intent.getStringExtra(Intent.EXTRA_TEXT).orEmpty()
            if (text.isNotBlank()) {
                sharedText = text
                if (::web.isInitialized) web.evaluateJavascript("window.hermesShared && window.hermesShared()", null)
            }
        }
    }

    private fun dispatchAgentJob(jobId: String) {
        if (!::web.isInitialized) return
        val quoted = org.json.JSONObject.quote(jobId)
        web.evaluateJavascript("window.hermesAgentJob && window.hermesAgentJob($quoted)", null)
    }

    private fun requestNotificationPermission() {
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), REQUEST_NOTIFICATIONS)
        }
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        web.evaluateJavascript("(window.hermesBack && window.hermesBack()) ? 'handled' : ''") { result ->
            if (result?.contains("handled") == true) return@evaluateJavascript
            if (web.canGoBack()) {
                web.goBack()
            } else {
                moveTaskToBack(true)
            }
        }
    }

    private fun canChooseFiles(): Boolean {
        val runtime = HermesRuntime.snapshot
        return !isFinishing && !isDestroyed && cookieInstalled && authenticatedPageReady &&
            runtime.phase == HermesRuntime.Phase.RUNNING && runtime.webCookie.isNotEmpty() &&
            runtime.port == loadedPort && runtime.startedAt == loadedStart &&
            AssetServer.isLocal(web.url?.let(Uri::parse), loadedPort)
    }

    private fun fileChooserError(message: String) {
        // Never log selected names, URIs, provider exceptions, or authentication data.
        Log.w("HermesFiles", message)
        if (!isFinishing && !isDestroyed) Toast.makeText(this, message, Toast.LENGTH_LONG).show()
    }

    private fun finishFileChooser(uris: Array<Uri>?) {
        val pending = fileChooser ?: return
        fileChooser = null
        pending.timeout?.let(mainHandler::removeCallbacks)
        pending.cancellation.cancel()
        pending.callback.onReceiveValue(uris)
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        if (requestCode != REQUEST_FILES) {
            @Suppress("DEPRECATION")
            super.onActivityResult(requestCode, resultCode, data)
            return
        }
        pickerLaunched = false
        val pending = fileChooser ?: return
        if (resultCode != RESULT_OK || !canChooseFiles() || pending.port != loadedPort ||
            pending.startedAt != loadedStart) {
            finishFileChooser(null)
            return
        }
        try {
            val uris = linkedSetOf<Uri>()
            data?.data?.let(uris::add)
            data?.clipData?.let { clip ->
                for (i in 0 until clip.itemCount) uris.add(clip.getItemAt(i).uri)
            }
            if (uris.isEmpty() || (!pending.multiple && uris.size != 1)) {
                finishFileChooser(null)
                fileChooserError("未收到有效文件，请重新选择")
                return
            }
            val selected = uris.toTypedArray()
            val timeout = Runnable {
                if (fileChooser === pending) {
                    finishFileChooser(null)
                    fileChooserError("读取文件超时；云端文件请先下载到本机后重试")
                }
            }
            pending.timeout = timeout
            mainHandler.postDelayed(timeout, 60_000L)
            fileReader.execute {
                val readable = try {
                    selected.forEach { uri ->
                        require(uri.scheme == "content" && !uri.authority.isNullOrBlank())
                        val provider = packageManager.resolveContentProvider(uri.authority!!, 0)
                        require(provider != null && provider.applicationInfo.uid != applicationInfo.uid)
                        require(checkUriPermission(uri, Process.myPid(), Process.myUid(),
                            Intent.FLAG_GRANT_READ_URI_PERMISSION) == PackageManager.PERMISSION_GRANTED)
                        pending.cancellation.throwIfCanceled()
                        // Probe actual bytes off the UI thread, including cloud hydration.
                        val descriptor = contentResolver.openAssetFileDescriptor(uri, "r", pending.cancellation)
                            ?: throw java.io.IOException("Missing document stream")
                        descriptor.use { asset ->
                            asset.createInputStream().use { stream ->
                                pending.cancellation.setOnCancelListener {
                                    try { stream.close() } catch (_: java.io.IOException) { }
                                }
                                try {
                                    pending.cancellation.throwIfCanceled()
                                    stream.read()
                                } finally {
                                    pending.cancellation.setOnCancelListener(null)
                                }
                            }
                        }
                    }
                    true
                } catch (_: Exception) {
                    false
                }
                mainHandler.post {
                    if (fileChooser === pending) {
                        if (readable && canChooseFiles() && pending.port == loadedPort &&
                            pending.startedAt == loadedStart) {
                            // Activity read grants last through WebView's upload. Do not
                            // persist grants or copy here; the upload endpoint owns the copy.
                            finishFileChooser(selected)
                        } else {
                            finishFileChooser(null)
                            fileChooserError("无法读取所选文件；请从系统“文件”重新选择，云端文件可先下载到本机")
                        }
                    }
                }
            }
        } catch (_: RuntimeException) {
            finishFileChooser(null)
            fileChooserError("文件选择结果无效，请重新选择")
        }
    }

    override fun onResume() {
        super.onResume()
        web.onResume()
        web.evaluateJavascript("window.hermesResume && window.hermesResume()", null)
    }

    override fun onPause() {
        web.onPause()
        super.onPause()
    }

    override fun onDestroy() {
        HermesRuntime.removeListener(runtimeListener)
        authenticatedPageReady = false
        finishFileChooser(null)
        mainHandler.removeCallbacksAndMessages(null)
        fileReader.shutdownNow()
        web.destroy()
        super.onDestroy()
    }

    private fun openExternal(uri: Uri) {
        try {
            startActivity(Intent(Intent.ACTION_VIEW, uri).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        } catch (_: ActivityNotFoundException) {
            Toast.makeText(this, "没有可以打开此链接的应用", Toast.LENGTH_SHORT).show()
        }
    }

    /** Controls for the shell page. Nothing here reveals secrets (see AssetServer). */
    inner class Bridge {
        @JavascriptInterface
        fun start() = HermesService.send(this@MainActivity, HermesService.ACTION_START)

        @JavascriptInterface
        fun stop() = HermesService.send(this@MainActivity, HermesService.ACTION_STOP)

        @JavascriptInterface
        fun restart() = HermesService.send(this@MainActivity, HermesService.ACTION_RESTART)

        @JavascriptInterface
        fun keepAwake(): Boolean = Prefs.keepAwake(this@MainActivity)

        @JavascriptInterface
        fun setKeepAwake(value: Boolean) = Prefs.setKeepAwake(this@MainActivity, value)

        @JavascriptInterface
        fun autoStart(): Boolean = Prefs.autoStart(this@MainActivity)

        @JavascriptInterface
        fun setAutoStart(value: Boolean) = Prefs.setAutoStart(this@MainActivity, value)

        @JavascriptInterface
        fun batteryOptimized(): Boolean {
            val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
            return !pm.isIgnoringBatteryOptimizations(packageName)
        }

        @SuppressLint("BatteryLife")
        @JavascriptInterface
        fun requestBatteryExemption() {
            runOnUiThread {
                try {
                    startActivity(
                        Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName")),
                    )
                } catch (_: ActivityNotFoundException) {
                    startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
                }
            }
        }

        @JavascriptInterface
        fun openAppSettings() {
            runOnUiThread {
                startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$packageName")))
            }
        }

        @JavascriptInterface
        fun openExternal(url: String) {
            val uri = Uri.parse(url)
            if (uri.scheme == "http" || uri.scheme == "https") runOnUiThread { this@MainActivity.openExternal(uri) }
        }

        @JavascriptInterface
        fun copyText(text: String) {
            runOnUiThread {
                val cm = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                cm.setPrimaryClip(ClipData.newPlainText("Hermes", text))
                Toast.makeText(this@MainActivity, "已复制", Toast.LENGTH_SHORT).show()
            }
        }

        @JavascriptInterface
        fun shareText(text: String) {
            runOnUiThread {
                val send = Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_TEXT, text)
                startActivity(Intent.createChooser(send, "分享"))
            }
        }

        @JavascriptInterface
        fun takeSharedText(): String {
            val text = sharedText
            sharedText = ""
            return text
        }

        @JavascriptInterface
        fun toast(text: String) {
            runOnUiThread { Toast.makeText(this@MainActivity, text, Toast.LENGTH_SHORT).show() }
        }

        @JavascriptInterface
        fun deviceInfo(): String = "Android ${Build.VERSION.RELEASE} (API ${Build.VERSION.SDK_INT}) · ${Build.MANUFACTURER} ${Build.MODEL}"

        @JavascriptInterface
        fun registerAgentJob(sessionId: String, prompt: String): String =
            AgentJobCoordinator.registerUiJob(sessionId, prompt)

        @JavascriptInterface
        fun activateAgentJob(jobId: String): Boolean = AgentJobCoordinator.activateJob(jobId)

        @JavascriptInterface
        fun scheduleAgentJob(sessionId: String, prompt: String, delayMs: Long, intervalMs: Long): String =
            AgentJobCoordinator.scheduleNativeJob(
                sessionId = sessionId,
                prompt = prompt,
                delayMs = delayMs,
                intervalMs = intervalMs.takeIf { it > 0L },
            )

        @JavascriptInterface
        fun resumeAgentJob(jobId: String): Boolean = AgentJobCoordinator.resumeJob(jobId)

        @JavascriptInterface
        fun pauseAgentJob(jobId: String): Boolean = AgentJobCoordinator.pauseJob(jobId)

        @JavascriptInterface
        fun cancelAgentJob(jobId: String): Boolean = AgentJobCoordinator.cancelJob(jobId)
    }

    companion object {
        private const val REQUEST_FILES = 7001
        private const val REQUEST_NOTIFICATIONS = 7002
        const val EXTRA_JOB_ID = "hermes_agent_job_id"
    }
}
