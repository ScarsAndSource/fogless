package com.example.fogless   // change to your package

import android.Manifest
import android.app.AlertDialog
import android.app.DownloadManager
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.os.Environment
import android.webkit.*
import android.widget.EditText
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity

class MainActivity : AppCompatActivity() {
    private val APP_URL = "https://YOUR-APP.vercel.app"
    private lateinit var web: WebView
    private var pendingMic: PermissionRequest? = null

    private val micLauncher = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        val req = pendingMic; pendingMic = null
        if (req != null) {
            if (granted) req.grant(arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE)) else req.deny()
        }
    }

    private fun sameOrigin(u: Uri?) = u != null && Uri.parse(APP_URL).let { it.scheme == u.scheme && it.host == u.host }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        web = WebView(this)
        setContentView(web)

        web.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            allowFileAccess = false
            allowContentAccess = false
            mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
            setSupportMultipleWindows(false)
            mediaPlaybackRequiresUserGesture = false
        }
        CookieManager.getInstance().setAcceptCookie(true)

        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                if (sameOrigin(request.url)) return false
                if (request.url.scheme == "https" || request.url.scheme == "http" || request.url.scheme == "mailto") {
                    startActivity(Intent(Intent.ACTION_VIEW, request.url))
                }
                return true // never load foreign pages inside the app
            }
        }

        web.webChromeClient = object : WebChromeClient() {
            override fun onPermissionRequest(request: PermissionRequest) {
                val wantsMic = request.resources.contains(PermissionRequest.RESOURCE_AUDIO_CAPTURE)
                if (!sameOrigin(request.origin) || !wantsMic) { request.deny(); return }
                if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
                    request.grant(arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE))
                } else {
                    pendingMic?.deny()
                    pendingMic = request
                    micLauncher.launch(Manifest.permission.RECORD_AUDIO)
                }
            }
            override fun onJsAlert(v: WebView, url: String, message: String, r: JsResult): Boolean {
                AlertDialog.Builder(this@MainActivity).setMessage(message)
                    .setPositiveButton("OK") { _, _ -> r.confirm() }.setOnCancelListener { r.cancel() }.show()
                return true
            }
            override fun onJsConfirm(v: WebView, url: String, message: String, r: JsResult): Boolean {
                AlertDialog.Builder(this@MainActivity).setMessage(message)
                    .setPositiveButton("OK") { _, _ -> r.confirm() }
                    .setNegativeButton("Cancel") { _, _ -> r.cancel() }.setOnCancelListener { r.cancel() }.show()
                return true
            }
            override fun onJsPrompt(v: WebView, url: String, message: String, default: String?, r: JsPromptResult): Boolean {
                val input = EditText(this@MainActivity).apply { setText(default ?: "") }
                AlertDialog.Builder(this@MainActivity).setMessage(message).setView(input)
                    .setPositiveButton("OK") { _, _ -> r.confirm(input.text.toString()) }
                    .setNegativeButton("Cancel") { _, _ -> r.cancel() }.setOnCancelListener { r.cancel() }.show()
                return true
            }
        }

        // CSV / JSON export: the session cookie must travel with the download request.
        web.setDownloadListener { url, userAgent, contentDisposition, mimeType, _ ->
            if (sameOrigin(Uri.parse(url))) {
                val req = DownloadManager.Request(Uri.parse(url))
                    .addRequestHeader("Cookie", CookieManager.getInstance().getCookie(url) ?: "")
                    .addRequestHeader("User-Agent", userAgent)
                    .setMimeType(mimeType)
                    .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                    .setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS,
                        URLUtil.guessFileName(url, contentDisposition, mimeType))
                (getSystemService(DOWNLOAD_SERVICE) as DownloadManager).enqueue(req)
            }
        }

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (web.canGoBack()) web.goBack() else { isEnabled = false; onBackPressedDispatcher.onBackPressed() }
            }
        })

        if (savedInstanceState == null) web.loadUrl("$APP_URL/") else web.restoreState(savedInstanceState)
    }

    override fun onSaveInstanceState(outState: Bundle) { super.onSaveInstanceState(outState); web.saveState(outState) }
}
