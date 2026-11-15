package com.vcorp.fogless.widget;

import android.app.PendingIntent;
import android.appwidget.AppWidgetManager;
import android.appwidget.AppWidgetProvider;
import android.content.Context;
import android.content.Intent;
import android.os.Bundle;
import android.util.TypedValue;
import android.view.View;
import android.widget.RemoteViews;

import com.vcorp.fogless.MainActivity;
import com.vcorp.fogless.R;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.Collections;
import java.util.Iterator;
import java.util.List;

/** Widget 2: balance / month stats / recent history (chosen per widget in InfoConfigActivity). */
public class InfoWidgetProvider extends AppWidgetProvider {
    static final String ACTION_REFRESH = "com.vcorp.fogless.widget.ACTION_REFRESH";

    private static final int MAX_ROWS = 5;
    private static final int[] ROW = {R.id.row1, R.id.row2, R.id.row3, R.id.row4, R.id.row5};
    private static final int[] ROW_L = {R.id.row1_l, R.id.row2_l, R.id.row3_l, R.id.row4_l, R.id.row5_l};
    private static final int[] ROW_R = {R.id.row1_r, R.id.row2_r, R.id.row3_r, R.id.row4_r, R.id.row5_r};

    // ------------------------------------------------------------------ lifecycle

    @Override
    public void onUpdate(Context ctx, AppWidgetManager mgr, int[] ids) {
        for (int id : ids) render(ctx, mgr, id);
        WidgetRefresher.ensurePeriodic(ctx);
        WidgetRefresher.enqueueRefresh(ctx);
    }

    @Override
    public void onReceive(Context ctx, Intent intent) {
        super.onReceive(ctx, intent);
        if (intent != null && ACTION_REFRESH.equals(intent.getAction())) {
            WidgetRefresher.enqueueRefresh(ctx);
        }
    }

    @Override
    public void onAppWidgetOptionsChanged(Context ctx, AppWidgetManager mgr, int id, Bundle newOptions) {
        super.onAppWidgetOptionsChanged(ctx, mgr, id, newOptions);
        render(ctx, mgr, id); // resized: show as many rows as now fit
    }

    @Override
    public void onDeleted(Context ctx, int[] ids) {
        WidgetStore.forget(ctx, ids);
        super.onDeleted(ctx, ids);
    }

    @Override
    public void onEnabled(Context ctx) {
        super.onEnabled(ctx);
        WidgetRefresher.ensurePeriodic(ctx);
    }

    @Override
    public void onDisabled(Context ctx) {
        WidgetRefresher.cancelPeriodic(ctx);
        WidgetStore.clearData(ctx); // last widget removed: don't keep a copy of the numbers around
        super.onDisabled(ctx);
    }

    // ------------------------------------------------------------------ rendering

    static void render(Context ctx, AppWidgetManager mgr, int id) {
        RemoteViews v = new RemoteViews(ctx.getPackageName(), R.layout.widget_info);
        v.setOnClickPendingIntent(R.id.info_root, openApp(ctx));
        v.setOnClickPendingIntent(R.id.info_refresh, refreshIntent(ctx, id));

        String mode = WidgetStore.getMode(ctx, id);
        boolean hide = WidgetStore.getHide(ctx, id);
        try {
            fill(ctx, mgr, id, v, mode, hide);
        } catch (Exception e) {
            message(v, "FOGLESS", "\u2014", "Couldn't read the data. Tap \u21BB.");
        }
        mgr.updateAppWidget(id, v);
    }

    private static void fill(Context ctx, AppWidgetManager mgr, int id, RemoteViews v, String mode, boolean hide) throws Exception {
        // defaults (RemoteViews is rebuilt from scratch every time, so set everything explicitly)
        v.setTextViewTextSize(R.id.info_big, TypedValue.COMPLEX_UNIT_SP, 28);
        v.setTextColor(R.id.info_big, Ui.INK);
        v.setViewVisibility(R.id.info_big, View.VISIBLE);
        v.setViewVisibility(R.id.info_sub, View.VISIBLE);
        for (int i = 0; i < MAX_ROWS; i++) v.setViewVisibility(ROW[i], View.GONE);

        if (WidgetStore.isSignedOut(ctx)) {
            message(v, "FOGLESS", "Signed out", "Open the app to sign in");
            return;
        }
        JSONObject st = WidgetStore.getState(ctx);
        if (st == null) {
            message(v, title(mode), "\u2014", "Loading\u2026");
            return;
        }

        long updated = WidgetStore.updatedAt(ctx);
        String stamp = updated > 0 ? Fmt.stamp(updated) : "";
        if (WidgetStore.isStale(ctx)) stamp = "offline \u00B7 " + stamp;
        v.setTextViewText(R.id.info_updated, stamp);
        v.setTextViewText(R.id.info_title, title(mode));

        double todaySpent = st.optDouble("today_spent", 0);
        int heightDp = heightDp(mgr, id);

        if (WidgetStore.MODE_HISTORY.equals(mode)) {
            v.setViewVisibility(R.id.info_big, View.GONE);
            v.setViewVisibility(R.id.info_sub, View.GONE);
            int fit = rowsThatFit(heightDp, false);
            JSONArray h = st.optJSONArray("history");
            int n = h == null ? 0 : Math.min(h.length(), fit);
            if (n == 0) {
                message(v, title(mode), "No entries yet", "Log your first expense");
                v.setTextViewText(R.id.info_updated, stamp);
                return;
            }
            for (int i = 0; i < n; i++) {
                JSONObject t = h.getJSONObject(i);
                String note = Fmt.str(t, "note");
                String desc = note.isEmpty() ? Fmt.cap(Fmt.str(t, "category")) : note;
                String pm = Fmt.pay(Fmt.str(t, "payment_method"));
                String left = pm.isEmpty() ? desc : desc + " \u00B7 " + pm;
                double amt = t.optDouble("amount", 0);
                String type = Fmt.str(t, "type");
                String right;
                int color;
                if ("expense".equals(type)) {
                    right = hide ? Fmt.DOTS : "\u2212" + Fmt.rupees(amt);
                    color = Ui.RED;
                } else if ("income".equals(type)) {
                    right = hide ? Fmt.DOTS : "+" + Fmt.rupees(amt);
                    color = Ui.GREEN;
                } else {
                    right = hide ? Fmt.DOTS : Fmt.rupees(amt);
                    color = Ui.MUTED;
                }
                row(v, i, left, right, color);
            }
            return;
        }

        int fit = rowsThatFit(heightDp, true);

        if (WidgetStore.MODE_STATS.equals(mode)) {
            JSONObject s = st.getJSONObject("stats");
            double spent = s.optDouble("total_expense", 0);
            double income = s.optDouble("total_income", 0);
            double net = s.optDouble("net", 0);
            v.setTextViewText(R.id.info_big, hide ? "\u20B9" + Fmt.DOTS : Fmt.money(spent));
            v.setTextViewText(R.id.info_sub, hide
                    ? "spent this month"
                    : "spent \u00B7 income " + Fmt.money(income) + " \u00B7 net " + Fmt.money(net));
            JSONArray cats = s.optJSONArray("by_category");
            int n = cats == null ? 0 : Math.min(cats.length(), fit);
            for (int i = 0; i < n; i++) {
                JSONObject c = cats.getJSONObject(i);
                String name = Fmt.cap(Fmt.str(c, "category"));
                row(v, i, name.isEmpty() ? "Other" : name,
                        hide ? Fmt.DOTS : Fmt.rupees(c.optDouble("total", 0)), Ui.INK);
            }
            return;
        }

        // balance
        double total = st.optDouble("balance", 0);
        v.setTextViewText(R.id.info_big, hide ? "\u20B9" + Fmt.DOTS : Fmt.money(total));
        v.setTextColor(R.id.info_big, total < -0.004 ? Ui.RED : Ui.INK);
        v.setTextViewText(R.id.info_sub, "today spent " + (hide ? Fmt.DOTS : Fmt.money(todaySpent)));
        JSONObject bal = WidgetStore.getBalances(ctx);
        JSONObject per = bal == null ? null : bal.optJSONObject("balances");
        if (per != null) {
            List<String> keys = new ArrayList<>();
            Iterator<String> it = per.keys();
            while (it.hasNext()) {
                String k = it.next();
                if (Math.abs(per.optDouble(k, 0)) > 0.004) keys.add(k);
            }
            final JSONObject src = per;
            Collections.sort(keys, (a, b) -> Double.compare(src.optDouble(b, 0), src.optDouble(a, 0)));
            int n = Math.min(keys.size(), fit);
            for (int i = 0; i < n; i++) {
                double val = per.optDouble(keys.get(i), 0);
                row(v, i, Fmt.pay(keys.get(i)), hide ? Fmt.DOTS : Fmt.money(val), val < 0 ? Ui.RED : Ui.INK);
            }
        }
    }

    private static void message(RemoteViews v, String title, String big, String sub) {
        v.setTextViewText(R.id.info_title, title);
        v.setTextViewText(R.id.info_updated, "");
        v.setTextViewTextSize(R.id.info_big, TypedValue.COMPLEX_UNIT_SP, 20);
        v.setTextColor(R.id.info_big, Ui.INK);
        v.setViewVisibility(R.id.info_big, View.VISIBLE);
        v.setTextViewText(R.id.info_big, big);
        v.setViewVisibility(R.id.info_sub, View.VISIBLE);
        v.setTextViewText(R.id.info_sub, sub);
        for (int i = 0; i < MAX_ROWS; i++) v.setViewVisibility(ROW[i], View.GONE);
    }

    private static void row(RemoteViews v, int i, String left, String right, int rightColor) {
        v.setViewVisibility(ROW[i], View.VISIBLE);
        v.setTextViewText(ROW_L[i], left);
        v.setTextViewText(ROW_R[i], right);
        v.setTextColor(ROW_R[i], rightColor);
    }

    private static String title(String mode) {
        if (WidgetStore.MODE_STATS.equals(mode)) return "THIS MONTH";
        if (WidgetStore.MODE_HISTORY.equals(mode)) return "RECENT";
        return "BALANCE";
    }

    private static int heightDp(AppWidgetManager mgr, int id) {
        try {
            Bundle o = mgr.getAppWidgetOptions(id);
            int h = o == null ? 0 : o.getInt(AppWidgetManager.OPTION_APPWIDGET_MIN_HEIGHT, 0);
            return h > 0 ? h : 120;
        } catch (RuntimeException e) {
            return 120;
        }
    }

    /** Rough vertical budget in dp: 24 padding + 20 header, 54 for big+sub, ~19 per row. */
    private static int rowsThatFit(int heightDp, boolean hasBigAndSub) {
        int free = heightDp - 24 - 20 - (hasBigAndSub ? 54 : 0);
        return Math.max(hasBigAndSub ? 0 : 1, Math.min(MAX_ROWS, free / 19));
    }

    // ------------------------------------------------------------------ intents

    private static PendingIntent openApp(Context ctx) {
        Intent i = new Intent(ctx, MainActivity.class);
        i.setAction(Intent.ACTION_MAIN);
        i.addCategory(Intent.CATEGORY_LAUNCHER);
        i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED);
        return PendingIntent.getActivity(ctx, 0, i, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
    }

    private static PendingIntent refreshIntent(Context ctx, int id) {
        Intent i = new Intent(ctx, InfoWidgetProvider.class);
        i.setAction(ACTION_REFRESH);
        return PendingIntent.getBroadcast(ctx, id, i, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
    }
}
