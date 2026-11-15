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
import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;

import com.getcapacitor.BridgeActivity;
import com.vcorp.fogless.widget.WidgetRefresher;

public class MainActivity extends BridgeActivity {
    private static final String APP_URL = "https://YOUR-APP.vercel.app";   // same as server.url
    private static final int LINEN = Color.parseColor("#FBF8F1");

    @Override
    public void onCreate(Bundle savedInstanceState) {
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
    }

    // 3) Persist the session cookies now, so the sign-in survives the OS killing the app.
    @Override
    public void onPause() {
        CookieManager.getInstance().flush();
        // Leaving the app (usually right after logging something): repaint the home-screen widgets.
        WidgetRefresher.requestNow(getApplicationContext());
        super.onPause();
    }
}
