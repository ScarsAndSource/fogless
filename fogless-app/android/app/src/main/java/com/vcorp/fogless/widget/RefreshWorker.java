package com.vcorp.fogless.widget;

import android.content.Context;

import androidx.annotation.NonNull;
import androidx.work.Worker;
import androidx.work.WorkerParameters;

/** Background refresh for the info widget (periodic + on demand). Always succeeds; the next run just tries again. */
public class RefreshWorker extends Worker {
    public RefreshWorker(@NonNull Context context, @NonNull WorkerParameters params) {
        super(context, params);
    }

    @NonNull
    @Override
    public Result doWork() {
        try {
            WidgetRefresher.refreshBlocking(getApplicationContext());
        } catch (Exception ignored) {
            // a widget refresh must never crash the app process
        }
        return Result.success();
    }
}
