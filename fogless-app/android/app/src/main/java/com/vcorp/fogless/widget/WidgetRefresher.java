package com.vcorp.fogless.widget;

import android.appwidget.AppWidgetManager;
import android.content.ComponentName;
import android.content.Context;

import androidx.work.Constraints;
import androidx.work.ExistingPeriodicWorkPolicy;
import androidx.work.ExistingWorkPolicy;
import androidx.work.NetworkType;
import androidx.work.OneTimeWorkRequest;
import androidx.work.PeriodicWorkRequest;
import androidx.work.WorkManager;

import java.util.concurrent.TimeUnit;

/** Fetches /api/state (+ /api/balances), caches them and repaints every info widget. */
public final class WidgetRefresher {
    private static final String PERIODIC = "fogless_widget_periodic";
    private static final String ONCE = "fogless_widget_once";
    private static final Object LOCK = new Object();

    private WidgetRefresher() {}

    private static Constraints needsNetwork() {
        return new Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build();
    }

    static boolean hasInfoWidgets(Context c) {
        try {
            int[] ids = AppWidgetManager.getInstance(c).getAppWidgetIds(new ComponentName(c, InfoWidgetProvider.class));
            return ids != null && ids.length > 0;
        } catch (RuntimeException e) {
            return false;
        }
    }

    /** Every 15 minutes while at least one info widget exists. KEEP = calling this again is harmless. */
    static void ensurePeriodic(Context c) {
        try {
            PeriodicWorkRequest r = new PeriodicWorkRequest.Builder(RefreshWorker.class, 15, TimeUnit.MINUTES)
                    .setConstraints(needsNetwork())
                    .build();
            WorkManager.getInstance(c.getApplicationContext())
                    .enqueueUniquePeriodicWork(PERIODIC, ExistingPeriodicWorkPolicy.KEEP, r);
        } catch (Exception ignored) {
            // WorkManager unavailable: widgets still refresh on tap and when the app is left
        }
    }

    static void cancelPeriodic(Context c) {
        try {
            WorkManager.getInstance(c.getApplicationContext()).cancelUniqueWork(PERIODIC);
        } catch (Exception ignored) {
            // nothing to cancel
        }
    }

    /** Refresh right now, but only if an info widget actually exists. Safe to call from anywhere. */
    public static void requestNow(Context c) {
        try {
            if (hasInfoWidgets(c)) enqueueRefresh(c);
        } catch (Exception ignored) {
            // never throw into the caller (MainActivity.onPause, activities)
        }
    }

    static void enqueueRefresh(Context c) {
        try {
            OneTimeWorkRequest r = new OneTimeWorkRequest.Builder(RefreshWorker.class)
                    .setConstraints(needsNetwork())
                    .build();
            WorkManager.getInstance(c.getApplicationContext())
                    .enqueueUniqueWork(ONCE, ExistingWorkPolicy.REPLACE, r);
        } catch (Exception ignored) {
            // see ensurePeriodic
        }
    }

    /** Blocking: network on the calling (background) thread. One refresh at a time. */
    static void refreshBlocking(Context ctx) {
        Context app = ctx.getApplicationContext();
        synchronized (LOCK) {
            FoglessApi.ApiResponse st = FoglessApi.get(app, "/api/state");
            if (st.ok()) {
                FoglessApi.ApiResponse bal = FoglessApi.get(app, "/api/balances");
                WidgetStore.saveData(app, st.json.toString(), bal.ok() ? bal.json.toString() : null);
            } else if (st.signedOut()) {
                WidgetStore.markSignedOut(app);
            } else {
                WidgetStore.markOffline(app);
            }
            updateAllInfoWidgets(app);
        }
    }

    static void updateAllInfoWidgets(Context app) {
        try {
            AppWidgetManager mgr = AppWidgetManager.getInstance(app);
            int[] ids = mgr.getAppWidgetIds(new ComponentName(app, InfoWidgetProvider.class));
            if (ids == null) return;
            for (int id : ids) InfoWidgetProvider.render(app, mgr, id);
        } catch (RuntimeException ignored) {
            // a bad render must not kill the worker
        }
    }
}
