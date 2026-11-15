package com.vcorp.fogless.widget;

import android.app.Activity;
import android.appwidget.AppWidgetManager;
import android.content.Intent;
import android.graphics.Typeface;
import android.os.Bundle;
import android.view.View;
import android.view.ViewGroup;
import android.widget.CheckBox;
import android.widget.LinearLayout;
import android.widget.RadioButton;
import android.widget.RadioGroup;

/** Shown when an info widget is added (and on long-press > reconfigure): pick what it displays. */
public class InfoConfigActivity extends Activity {
    private int widgetId = AppWidgetManager.INVALID_APPWIDGET_ID;
    private RadioButton rbBalance;
    private RadioButton rbStats;
    private RadioButton rbHistory;
    private CheckBox cbHide;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setResult(RESULT_CANCELED); // backing out must not add the widget

        Intent in = getIntent();
        Bundle extras = in == null ? null : in.getExtras();
        if (extras != null) {
            widgetId = extras.getInt(AppWidgetManager.EXTRA_APPWIDGET_ID, AppWidgetManager.INVALID_APPWIDGET_ID);
        }
        if (widgetId == AppWidgetManager.INVALID_APPWIDGET_ID) {
            finish();
            return;
        }

        Ui.transparentWindow(this);
        LinearLayout root = Ui.card(this);
        root.addView(Ui.label(this, "WHAT SHOULD THIS WIDGET SHOW?", 12, Ui.MUTED, true));

        RadioGroup group = new RadioGroup(this);
        group.setOrientation(RadioGroup.VERTICAL);
        rbBalance = radio("Balance  \u00B7  total + per method");
        rbStats = radio("This month  \u00B7  spent, income, top categories");
        rbHistory = radio("Recent history  \u00B7  last entries");
        group.addView(rbBalance);
        group.addView(rbStats);
        group.addView(rbHistory);
        LinearLayout.LayoutParams gp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        gp.topMargin = Ui.dp(this, 10);
        root.addView(group, gp);

        cbHide = new CheckBox(this);
        cbHide.setText("Hide amounts (show \u2022\u2022\u2022\u2022)");
        cbHide.setTextColor(Ui.INK);
        cbHide.setTypeface(Typeface.MONOSPACE);
        cbHide.setTextSize(13);
        root.addView(cbHide, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        // current settings (defaults for a brand-new widget)
        String mode = WidgetStore.getMode(this, widgetId);
        rbBalance.setChecked(WidgetStore.MODE_BALANCE.equals(mode));
        rbStats.setChecked(WidgetStore.MODE_STATS.equals(mode));
        rbHistory.setChecked(WidgetStore.MODE_HISTORY.equals(mode));
        cbHide.setChecked(WidgetStore.getHide(this, widgetId));

        android.widget.Button save = Ui.button(this, "Save", true);
        save.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                save();
            }
        });
        LinearLayout.LayoutParams sp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        sp.topMargin = Ui.dp(this, 14);
        root.addView(save, sp);

        setContentView(root);
        Ui.sizeDialog(this);
    }

    private RadioButton radio(String text) {
        RadioButton rb = new RadioButton(this);
        rb.setId(View.generateViewId());
        rb.setText(text);
        rb.setTextColor(Ui.INK);
        rb.setTypeface(Typeface.MONOSPACE);
        rb.setTextSize(13);
        return rb;
    }

    private void save() {
        String mode = WidgetStore.MODE_BALANCE;
        if (rbStats.isChecked()) mode = WidgetStore.MODE_STATS;
        else if (rbHistory.isChecked()) mode = WidgetStore.MODE_HISTORY;
        WidgetStore.setSettings(this, widgetId, mode, cbHide.isChecked());

        // With a configure activity the system does NOT send the first update: paint it ourselves.
        AppWidgetManager mgr = AppWidgetManager.getInstance(this);
        InfoWidgetProvider.render(this, mgr, widgetId);
        WidgetRefresher.ensurePeriodic(this);
        WidgetRefresher.enqueueRefresh(this);

        Intent result = new Intent();
        result.putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, widgetId);
        setResult(RESULT_OK, result);
        finish();
    }
}
