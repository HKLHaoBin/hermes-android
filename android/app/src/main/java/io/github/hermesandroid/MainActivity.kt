package io.github.hermesandroid

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
    private lateinit var dashboardWeb: WebView
    private lateinit var body: android.widget.FrameLayout
    private var showDashboard = false
    private var loadedDashboard = 0
    private var loadedDashboardStart = 0L
    @Volatile private var loadedPort = 0
    private var loadedStart = 0L
    private lateinit var statusLabel: android.widget.TextView
    private var fileCallback: ValueCallback<Array<Uri>>? = null
    @Volatile private var sharedText: String = ""

    private val runtimeListener: (HermesRuntime.Snapshot) -> Unit = { s ->
        runOnUiThread {
            if (!isFinishing && !isDestroyed) renderRuntime(HermesRuntime.snapshot)
        }
    }

    @SuppressLint("SetJavaScriptEnabled", "JavascriptInterface")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG)

        web = WebView(this)
        web.setBackgroundColor(Color.parseColor("#F5F4EF"))
        statusLabel = android.widget.TextView(this).apply {
            gravity = android.view.Gravity.CENTER
            textSize = 16f
            setPadding(24, 24, 24, 24)
        }
        dashboardWeb = WebView(this).apply {
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.allowFileAccess = false
            settings.allowContentAccess = false
            webViewClient = object : WebViewClient() {
                override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                    val url = request.url
                    if (url.scheme == "http" && url.host == "127.0.0.1" && url.port == loadedDashboard) return false
                    if (request.isForMainFrame) openExternal(url)
                    return true
                }
            }
        }
        body = android.widget.FrameLayout(this).apply {
            addView(web, -1, -1)
            addView(dashboardWeb, -1, -1)
            addView(statusLabel, -1, -1)
        }
        val tabs = android.widget.LinearLayout(this)
        fun tab(label: String, dashboard: Boolean) {
            tabs.addView(android.widget.Button(this).apply {
                text = label
                setOnClickListener {
                    showDashboard = dashboard
                    renderRuntime(HermesRuntime.snapshot)
                }
            }, android.widget.LinearLayout.LayoutParams(0, -2, 1f))
        }
        tab("聊天", false)
        tab("官方设置", true)
        setContentView(android.widget.LinearLayout(this).apply {
            orientation = android.widget.LinearLayout.VERTICAL
            addView(tabs, -1, -2)
            addView(body, android.widget.LinearLayout.LayoutParams(-1, 0, 1f))
        })

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
                fileCallback?.onReceiveValue(null)
                fileCallback = callback
                val intent = params.createIntent().apply {
                    addCategory(Intent.CATEGORY_OPENABLE)
                    if (params.mode == FileChooserParams.MODE_OPEN_MULTIPLE) {
                        putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true)
                    }
                }
                return try {
                    startActivityForResult(intent, REQUEST_FILES)
                    true
                } catch (_: ActivityNotFoundException) {
                    fileCallback = null
                    callback.onReceiveValue(null)
                    false
                }
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

    private fun renderRuntime(s: HermesRuntime.Snapshot) {
        val running = s.phase == HermesRuntime.Phase.RUNNING
        val dashboardReady = running && s.dashboardPort > 0 && s.dashboardCookie.isNotEmpty()
        dashboardWeb.visibility = if (showDashboard && dashboardReady) android.view.View.VISIBLE else android.view.View.INVISIBLE
        web.visibility = if (!showDashboard && running) android.view.View.VISIBLE else android.view.View.INVISIBLE
        if (!dashboardReady && loadedDashboard != 0) {
            loadedDashboard = 0
            dashboardWeb.loadUrl("about:blank")
        }
        if (showDashboard) {
            statusLabel.visibility = if (dashboardReady) android.view.View.GONE else android.view.View.VISIBLE
            statusLabel.text = if (s.dashboardError) "官方设置启动失败，聊天仍可使用。" else "正在准备官方设置…"
            if (!dashboardReady || (loadedDashboard == s.dashboardPort && loadedDashboardStart == s.startedAt)) return
            loadedDashboard = s.dashboardPort
            loadedDashboardStart = s.startedAt
            val origin = "http://127.0.0.1:${s.dashboardPort}/"
            CookieManager.getInstance().setCookie(origin, s.dashboardCookie) { accepted ->
                if (isDestroyed || loadedDashboard != s.dashboardPort || loadedDashboardStart != s.startedAt) return@setCookie
                if (accepted) dashboardWeb.loadUrl(origin) else {
                    loadedDashboard = 0
                    if (showDashboard) {
                        statusLabel.visibility = android.view.View.VISIBLE
                        statusLabel.text = "无法建立官方设置会话。"
                    }
                }
            }
            return
        }
        if (s.phase != HermesRuntime.Phase.RUNNING || s.webCookie.isEmpty()) {
            if (loadedPort != 0) web.loadUrl("about:blank")
            loadedPort = 0
            loadedStart = 0L
            statusLabel.text = s.message.ifBlank { "正在准备手机端 Hermes…" }
            statusLabel.visibility = android.view.View.VISIBLE
            web.visibility = android.view.View.INVISIBLE
            return
        }
        if (loadedPort == s.port && loadedStart == s.startedAt) {
            statusLabel.visibility = android.view.View.GONE
            return
        }
        loadedPort = s.port
        loadedStart = s.startedAt
        val origin = "http://127.0.0.1:${s.port}/"
        CookieManager.getInstance().setCookie(origin, s.webCookie) { accepted ->
            if (isDestroyed || loadedStart != s.startedAt || loadedPort != s.port) return@setCookie
            if (!accepted) {
                statusLabel.text = "无法建立 Hermes 本地会话，请重开应用。"
                return@setCookie
            }
            if (!showDashboard) {
                statusLabel.visibility = android.view.View.GONE
                web.visibility = android.view.View.VISIBLE
            }
            web.loadUrl(origin)
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleIntent(intent)
    }

    private fun handleIntent(intent: Intent?) {
        if (intent?.action == Intent.ACTION_SEND) {
            val text = intent.getStringExtra(Intent.EXTRA_TEXT).orEmpty()
            if (text.isNotBlank()) {
                sharedText = text
                if (::web.isInitialized) web.evaluateJavascript("window.hermesShared && window.hermesShared()", null)
            }
        }
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (showDashboard) {
            if (dashboardWeb.canGoBack()) dashboardWeb.goBack() else {
                showDashboard = false
                renderRuntime(HermesRuntime.snapshot)
            }
            return
        }
        web.evaluateJavascript("(window.hermesBack && window.hermesBack()) ? 'handled' : ''") { result ->
            if (result?.contains("handled") == true) return@evaluateJavascript
            if (web.canGoBack()) {
                web.goBack()
            } else {
                moveTaskToBack(true)
            }
        }
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        if (requestCode == REQUEST_FILES) {
            val callback = fileCallback
            fileCallback = null
            if (callback == null) return
            var result = WebChromeClient.FileChooserParams.parseResult(resultCode, data)
            val clip = data?.clipData
            if (resultCode == RESULT_OK && clip != null && clip.itemCount > 1) {
                result = Array(clip.itemCount) { clip.getItemAt(it).uri }
            }
            callback.onReceiveValue(result)
            return
        }
        @Suppress("DEPRECATION")
        super.onActivityResult(requestCode, resultCode, data)
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
        dashboardWeb.destroy()
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
    }

    companion object {
        private const val REQUEST_FILES = 7001
    }
}
