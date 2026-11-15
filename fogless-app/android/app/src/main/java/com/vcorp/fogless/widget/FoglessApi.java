package com.vcorp.fogless.widget;

import android.content.Context;
import android.webkit.CookieManager;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;

/**
 * Native HTTP client for the widgets.
 *
 * Auth model: the app keeps its session in two HttpOnly cookies (fl_at / fl_rt) inside the WebView's CookieManager.
 * We read them from there and send them as a Cookie header. If the server refreshes the session it answers with
 * Set-Cookie; we write those straight back into the CookieManager, so the WebView and the widgets always share
 * ONE session (Supabase rotates refresh tokens, so a private copy here would sign the app out).
 *
 * The base URL comes from capacitor.config.json (server.url), so it can never drift from what the app loads.
 * Nothing here ever follows a redirect or talks to any other host.
 */
public final class FoglessApi {
    private static final String COOKIE_AT = "fl_at=";
    private static final String COOKIE_RT = "fl_rt=";
    private static String sBase;

    private FoglessApi() {}

    public static final class ApiResponse {
        public final int status;         // HTTP status, or 0 when no response arrived
        public final JSONObject json;    // parsed body, null if it was not a JSON object
        public final boolean maybeSent;  // true when a POST may have reached the server before we lost it
        private final String note;

        ApiResponse(int status, JSONObject json, boolean maybeSent, String note) {
            this.status = status;
            this.json = json;
            this.maybeSent = maybeSent;
            this.note = note;
        }

        public static ApiResponse failure(String note) {
            return new ApiResponse(0, null, false, note);
        }

        /** 2xx, JSON body, and the server did not say ok:false. */
        public boolean ok() {
            return status >= 200 && status < 300 && json != null && json.optBoolean("ok", true);
        }

        public boolean signedOut() {
            return status == 401;
        }

        /** One human-readable line (or a short paragraph for command output). */
        public String message() {
            if (status == 401) return "Signed out. Open Fogless once to sign in.";
            if (status == 0) {
                if (maybeSent) return "Lost the connection mid-request. Check History in the app before retrying.";
                return note != null ? note : "No connection. Nothing was logged.";
            }
            String t = Fmt.str(json, "text");
            if (!t.isEmpty()) return t;
            if (status >= 500) return "Server error (HTTP " + status + ").";
            if (status >= 400) return "Request failed (HTTP " + status + ").";
            return "Done.";
        }
    }

    // ------------------------------------------------------------------ public calls

    public static ApiResponse get(Context ctx, String path) {
        return call(ctx, "GET", path, null, null);
    }

    public static ApiResponse postJson(Context ctx, String path, JSONObject body) {
        return call(ctx, "POST", path, "application/json; charset=utf-8", body.toString().getBytes(StandardCharsets.UTF_8));
    }

    public static ApiResponse postMultipart(Context ctx, String path, String field, String filename, String mime, byte[] data) {
        String boundary = "----FoglessWidget" + Long.toHexString(System.nanoTime());
        ByteArrayOutputStream bo = new ByteArrayOutputStream(data.length + 512);
        put(bo, "--" + boundary + "\r\n");
        put(bo, "Content-Disposition: form-data; name=\"" + field + "\"; filename=\"" + filename + "\"\r\n");
        put(bo, "Content-Type: " + mime + "\r\n\r\n");
        bo.write(data, 0, data.length);
        put(bo, "\r\n--" + boundary + "--\r\n");
        return call(ctx, "POST", path, "multipart/form-data; boundary=" + boundary, bo.toByteArray());
    }

    // ------------------------------------------------------------------ internals

    private static void put(ByteArrayOutputStream bo, String s) {
        byte[] b = s.getBytes(StandardCharsets.UTF_8);
        bo.write(b, 0, b.length);
    }

    private static synchronized String baseUrl(Context ctx) {
        if (sBase != null) return sBase;
        try (InputStream in = ctx.getApplicationContext().getAssets().open("capacitor.config.json")) {
            JSONObject cfg = new JSONObject(readAll(in, 64 * 1024));
            JSONObject server = cfg.optJSONObject("server");
            String url = server == null ? "" : server.optString("url", "").trim();
            while (url.endsWith("/")) url = url.substring(0, url.length() - 1);
            if (url.startsWith("https://") && !url.contains("YOUR-APP")) sBase = url;
        } catch (Exception ignored) {
            // falls through to null -> "not configured"
        }
        return sBase;
    }

    private static String readAll(InputStream in, int limit) throws IOException {
        ByteArrayOutputStream bo = new ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while ((n = in.read(buf)) != -1) {
            if (bo.size() + n > limit) {
                bo.write(buf, 0, Math.max(0, limit - bo.size()));
                break;
            }
            bo.write(buf, 0, n);
        }
        return new String(bo.toByteArray(), StandardCharsets.UTF_8);
    }

    private static String cookieHeader(String url) {
        try {
            return CookieManager.getInstance().getCookie(url);
        } catch (RuntimeException e) {
            return null;
        }
    }

    /** Persist refreshed session cookies exactly as the WebView would have. */
    private static void applyCookies(String url, Map<String, List<String>> headers) {
        if (headers == null) return;
        try {
            CookieManager cm = CookieManager.getInstance();
            boolean any = false;
            for (Map.Entry<String, List<String>> e : headers.entrySet()) {
                if (e.getKey() == null || !"set-cookie".equalsIgnoreCase(e.getKey()) || e.getValue() == null) continue;
                for (String c : e.getValue()) {
                    if (c != null && (c.startsWith(COOKIE_AT) || c.startsWith(COOKIE_RT))) {
                        cm.setCookie(url, c);
                        any = true;
                    }
                }
            }
            if (any) cm.flush();
        } catch (RuntimeException ignored) {
            // never let cookie housekeeping break a request
        }
    }

    private static ApiResponse call(Context ctx, String method, String path, String contentType, byte[] body) {
        String base = baseUrl(ctx);
        if (base == null) {
            return ApiResponse.failure("Fogless URL is not set in capacitor.config.json (server.url).");
        }
        String full = base + path;
        String cookieBefore = cookieHeader(full);
        ApiResponse r = once(full, method, contentType, body);
        if (r.status == 401) {
            // Another request (usually the WebView) may have just rotated the refresh token and written new
            // cookies. If they changed, try once more with them; if not, we really are signed out.
            String cookieNow = cookieHeader(full);
            boolean changed = cookieNow != null && !cookieNow.equals(cookieBefore);
            if (changed) {
                try {
                    Thread.sleep(300);
                } catch (InterruptedException ie) {
                    Thread.currentThread().interrupt();
                    return r;
                }
                r = once(full, method, contentType, body);
            }
        }
        return r;
    }

    private static ApiResponse once(String full, String method, String contentType, byte[] body) {
        HttpURLConnection conn = null;
        boolean sent = false;
        try {
            URL url = new URL(full);
            conn = (HttpURLConnection) url.openConnection();
            conn.setInstanceFollowRedirects(false);
            conn.setConnectTimeout(10000);
            conn.setReadTimeout(30000);
            conn.setRequestMethod(method);
            conn.setRequestProperty("Accept", "application/json");
            conn.setRequestProperty("User-Agent", "FoglessWidget/1.0 (Android)");
            String cookie = cookieHeader(full);
            if (cookie != null && !cookie.isEmpty()) conn.setRequestProperty("Cookie", cookie);
            if (body != null) {
                conn.setDoOutput(true);
                conn.setRequestProperty("Content-Type", contentType);
                conn.setFixedLengthStreamingMode(body.length);
                try (OutputStream os = conn.getOutputStream()) {
                    os.write(body);
                    os.flush();
                }
                sent = true;
            }

            int status;
            try {
                status = conn.getResponseCode();
            } catch (IOException first) {
                // Some Android versions throw on a bare 401 (no WWW-Authenticate); the code is there on a 2nd ask.
                status = conn.getResponseCode();
            }
            applyCookies(full, conn.getHeaderFields());

            InputStream is = status >= 400 ? conn.getErrorStream() : conn.getInputStream();
            String text = "";
            if (is != null) {
                try {
                    text = readAll(is, 1024 * 1024);
                } finally {
                    try {
                        is.close();
                    } catch (IOException ignored) {
                        // nothing to do
                    }
                }
            }
            JSONObject json = null;
            if (!text.isEmpty()) {
                try {
                    json = new JSONObject(text);
                } catch (JSONException ignored) {
                    // non-JSON body (HTML error page, etc.)
                }
            }
            return new ApiResponse(status, json, false, null);
        } catch (IOException e) {
            return new ApiResponse(0, null, sent, null);
        } catch (RuntimeException e) {
            return new ApiResponse(0, null, sent, null);
        } finally {
            if (conn != null) conn.disconnect();
        }
    }
}
