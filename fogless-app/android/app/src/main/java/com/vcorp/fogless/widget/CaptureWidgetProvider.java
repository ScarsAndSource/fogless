package com.vcorp.fogless.widget;

import android.app.PendingIntent;
import android.appwidget.AppWidgetManager;
import android.appwidget.AppWidgetProvider;
import android.content.Context;
import android.content.Intent;
import android.widget.RemoteViews;

import com.vcorp.fogless.R;

/** Widget 1: a fake text field + mic button. Each opens a tiny floating native screen (no WebView, instant). */
public class CaptureWidgetProvider extends AppWidgetProvider {

    @Override
    public void onUpdate(Context ctx, AppWidgetManager mgr, int[] ids) {
        for (int id : ids) mgr.updateAppWidget(id, build(ctx));
    }

    private static RemoteViews build(Context ctx) {
        RemoteViews v = new RemoteViews(ctx.getPackageName(), R.layout.widget_capture);
        v.setOnClickPendingIntent(R.id.cap_text, launch(ctx, QuickTextActivity.class, 1));
        v.setOnClickPendingIntent(R.id.cap_mic, launch(ctx, VoiceCaptureActivity.class, 2));
        return v;
    }

    private static PendingIntent launch(Context ctx, Class<?> target, int requestCode) {
        Intent i = new Intent(ctx, target);
        // fresh instance every tap, in its own task so the main app is never dragged to the front
        i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TASK);
        return PendingIntent.getActivity(ctx, requestCode, i, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
    }
}
