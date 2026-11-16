package com.vcorp.fogless;

import android.app.DownloadManager;
import android.content.Context;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.os.Environment;
import android.view.View;
import android.webkit.CookieManager;
import android.webkit.URLUtil;
import android.webkit.WebView;
import android.widget.Toast;

import androidx.activity.EdgeToEdge;
import androidx.activity.OnBackPressedCallback;
import androidx.appcompat.app.AppCompatDelegate;
import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;

import com.getcapacitor.BridgeActivity;
import com.vcorp.fogless.widget.WidgetRefresher;

public class MainActivity extends BridgeActivity {
    private static final String APP_URL = "https://fogless-psi.vercel.app";   // same as server.url
    private static final int LINEN = Color.parseColor("#FBF8F1");

    // Back gesture / button: close the page's open overlay first, otherwise go back or leave the app.
    private static final String BACK_JS =
            "(function(){"
            + "var p=document.getElementById('right-panel');"
            + "if(p&&p.classList.contains('open')){var c=document.getElementById('panel-close');if(c){c.click();}return 'panel';}"
            + "var d=document.getElementById('slash-suggest-drawer');"
            + "if(d&&!d.classList.contains('hidden')){var x=document.getElementById('drawer-close-btn');if(x){x.click();return 'panel';}}"
            + "var q=location.pathname;"
            + "if(q===''||q==='/'||q==='/index.html'||q==='/login'||q==='/login.html'){return 'exit';}"
            + "return 'back';"
            + "})()";

    @Override
    public void onCreate(Bundle savedInstanceState) {
        // The web UI is light-only (linen). Without this, a dark-mode phone gets white status/nav-bar icons on a
        // linen page (invisible) and WebView may try to auto-darken the page.
        AppCompatDelegate.setDefaultNightMode(AppCompatDelegate.MODE_NIGHT_NO);
        EdgeToEdge.enable(this);
        super.onCreate(savedInstanceState);

        // 1) Keep the page clear of the status bar, navigation bar, camera cut-out and keyboard.
        View root = findViewById(android.R.id.content);
        root.setBackgroundColor(LINEN);
        ViewCompat.setOnApplyWindowInsetsListener(root, (v, insets) -> {
            Insets bars = insets.getInsets(WindowInsetsCompat.Type.systemBars()
                    | WindowInsetsCompat.Type.displayCutout()
                    | WindowInsetsCompat.Type.ime());
            v.setPadding(bars.left, bars.top, bars.right, bars.bottom);
            return WindowInsetsCompat.CONSUMED;
        });

        // 2) Export CSV / Backup JSON: send the session cookie with the download.
        WebView web = getBridge().getWebView();
        web.setDownloadListener((url, userAgent, contentDisposition, mimeType, contentLength) -> {
            Uri u = Uri.parse(url);
            Uri base = Uri.parse(APP_URL);
            if (!"https".equals(u.getScheme()) || u.getHost() == null || !u.getHost().equals(base.getHost())) return;
            try {
                String cookie = CookieManager.getInstance().getCookie(url);
                DownloadManager.Request req = new DownloadManager.Request(u)
                        .addRequestHeader("Cookie", cookie == null ? "" : cookie)
                        .addRequestHeader("User-Agent", userAgent)
                        .setMimeType(mimeType)
                        .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                        .setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS,
                                URLUtil.guessFileName(url, contentDisposition, mimeType));
                ((DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE)).enqueue(req);
                Toast.makeText(this, "Download started", Toast.LENGTH_SHORT).show();
            } catch (Exception e) {
                Toast.makeText(this, "Could not start the download.", Toast.LENGTH_LONG).show();
            }
        });

        // 3) Back gesture / button.
        getOnBackPressedDispatcher().addCallback(this, new OnBackPressedCallback(true) {
            @Override
            public void handleOnBackPressed() {
                handleBack();
            }
        });
    }

    private void handleBack() {
        final WebView web = getBridge() == null ? null : getBridge().getWebView();
        if (web == null) {
            moveTaskToBack(true);
            return;
        }
        try {
            web.evaluateJavascript(BACK_JS, value -> {
                String v = value == null ? "" : value;
                if (v.contains("panel")) return;                       // the page closed its own overlay
                if (v.contains("back") && web.canGoBack()) {
                    web.goBack();
                    return;
                }
                moveTaskToBack(true);                                  // keep the app (and widgets) warm
            });
        } catch (RuntimeException e) {
            moveTaskToBack(true);
        }
    }

    // 4) Persist the session cookies now, so the sign-in survives the OS killing the app.
    @Override
    public void onPause() {
        CookieManager.getInstance().flush();
        // Leaving the app (usually right after logging something): repaint the home-screen widgets.
        WidgetRefresher.requestNow(getApplicationContext());
        super.onPause();
    }
}
