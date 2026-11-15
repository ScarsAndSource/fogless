package com.vcorp.fogless.widget;

import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONObject;

/** Last-known data + per-widget settings. Private app storage only (allowBackup is off in the manifest). */
final class WidgetStore {
    static final String MODE_BALANCE = "balance";
    static final String MODE_STATS = "stats";
    static final String MODE_HISTORY = "history";

    private static final String PREFS = "fogless_widget";
    private static final String K_STATE = "state_json";
    private static final String K_BALANCES = "balances_json";
    private static final String K_UPDATED = "updated_at";
    private static final String K_STALE = "stale";
    private static final String K_SIGNED_OUT = "signed_out";

    private WidgetStore() {}

    private static SharedPreferences p(Context c) {
        return c.getApplicationContext().getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    // ---- per-widget settings

    static String getMode(Context c, int id) {
        String m = p(c).getString("mode_" + id, MODE_BALANCE);
        if (MODE_STATS.equals(m) || MODE_HISTORY.equals(m)) return m;
        return MODE_BALANCE;
    }

    static boolean getHide(Context c, int id) {
        return p(c).getBoolean("hide_" + id, false);
    }

    static void setSettings(Context c, int id, String mode, boolean hide) {
        p(c).edit().putString("mode_" + id, mode).putBoolean("hide_" + id, hide).apply();
    }

    static void forget(Context c, int[] ids) {
        if (ids == null) return;
        SharedPreferences.Editor e = p(c).edit();
        for (int id : ids) e.remove("mode_" + id).remove("hide_" + id);
        e.apply();
    }

    // ---- cached data

    /** balancesJson may be null (keeps the previous per-method balances). */
    static void saveData(Context c, String stateJson, String balancesJson) {
        SharedPreferences.Editor e = p(c).edit()
                .putString(K_STATE, stateJson)
                .putLong(K_UPDATED, System.currentTimeMillis())
                .putBoolean(K_STALE, false)
                .putBoolean(K_SIGNED_OUT, false);
        if (balancesJson != null) e.putString(K_BALANCES, balancesJson);
        e.apply();
    }

    /** Last refresh failed (network / server): keep showing the old numbers, flagged as offline. */
    static void markOffline(Context c) {
        p(c).edit().putBoolean(K_STALE, true).apply();
    }

    /** Session is gone: drop the cached numbers so nothing financial stays on the home screen. */
    static void markSignedOut(Context c) {
        p(c).edit().remove(K_STATE).remove(K_BALANCES).remove(K_UPDATED)
                .putBoolean(K_STALE, false).putBoolean(K_SIGNED_OUT, true).apply();
    }

    static void clearData(Context c) {
        p(c).edit().remove(K_STATE).remove(K_BALANCES).remove(K_UPDATED)
                .putBoolean(K_STALE, false).putBoolean(K_SIGNED_OUT, false).apply();
    }

    static JSONObject getState(Context c) {
        return parse(p(c).getString(K_STATE, null));
    }

    static JSONObject getBalances(Context c) {
        return parse(p(c).getString(K_BALANCES, null));
    }

    static long updatedAt(Context c) {
        return p(c).getLong(K_UPDATED, 0L);
    }

    static boolean isStale(Context c) {
        return p(c).getBoolean(K_STALE, false);
    }

    static boolean isSignedOut(Context c) {
        return p(c).getBoolean(K_SIGNED_OUT, false);
    }

    private static JSONObject parse(String s) {
        if (s == null || s.isEmpty()) return null;
        try {
            return new JSONObject(s);
        } catch (Exception e) {
            return null;
        }
    }
}
