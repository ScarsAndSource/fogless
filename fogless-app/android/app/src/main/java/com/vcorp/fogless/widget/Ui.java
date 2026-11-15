package com.vcorp.fogless.widget;

import android.app.Activity;
import android.content.Context;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.ColorDrawable;
import android.graphics.drawable.GradientDrawable;
import android.util.DisplayMetrics;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

/** Tiny helpers so the little native dialogs match the app's linen / sage look without any layout XML. */
final class Ui {
    static final int LINEN = 0xFFFBF8F1;
    static final int INK = 0xFF243528;
    static final int MUTED = 0xFF716450;
    static final int SAGE_DARK = 0xFF2A4533;
    static final int SAGE_LIGHT = 0xFFE5F2E7;
    static final int BORDER = 0xFFD7CDBB;
    static final int RED = 0xFFA93F32;
    static final int GREEN = 0xFF2E7D4F;

    private Ui() {}

    static int dp(Context c, float v) {
        return Math.round(v * c.getResources().getDisplayMetrics().density);
    }

    static GradientDrawable rounded(Context c, int fill, int stroke, float radiusDp) {
        GradientDrawable d = new GradientDrawable();
        d.setShape(GradientDrawable.RECTANGLE);
        d.setColor(fill);
        d.setCornerRadius(dp(c, radiusDp));
        if (stroke != 0) d.setStroke(dp(c, 1), stroke);
        return d;
    }

    static TextView label(Context c, String text, float sp, int color, boolean bold) {
        TextView t = new TextView(c);
        t.setText(text);
        t.setTextSize(sp);
        t.setTextColor(color);
        t.setTypeface(Typeface.MONOSPACE, bold ? Typeface.BOLD : Typeface.NORMAL);
        return t;
    }

    static Button button(Context c, String text, boolean primary) {
        Button b = new Button(c);
        b.setText(text);
        b.setAllCaps(false);
        b.setTextSize(14);
        b.setTypeface(Typeface.MONOSPACE, Typeface.BOLD);
        b.setTextColor(primary ? Color.WHITE : INK);
        b.setBackground(rounded(c, primary ? SAGE_DARK : 0xFFFFFFFF, primary ? 0 : BORDER, 12));
        b.setStateListAnimator(null);
        b.setMinHeight(0);
        b.setMinimumHeight(dp(c, 44));
        b.setPadding(dp(c, 18), dp(c, 8), dp(c, 18), dp(c, 8));
        return b;
    }

    /** Vertical card used as the content of the floating (dialog-themed) activities. */
    static LinearLayout card(Context c) {
        LinearLayout root = new LinearLayout(c);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackground(rounded(c, LINEN, BORDER, 20));
        root.setPadding(dp(c, 20), dp(c, 18), dp(c, 20), dp(c, 16));
        return root;
    }

    /** Call BEFORE setContentView: lets our rounded card be the only visible surface. */
    static void transparentWindow(Activity a) {
        a.getWindow().setBackgroundDrawable(new ColorDrawable(Color.TRANSPARENT));
    }

    /** Call AFTER setContentView: ~92% of the screen width, capped for tablets. */
    static void sizeDialog(Activity a) {
        DisplayMetrics dm = a.getResources().getDisplayMetrics();
        int w = Math.min(Math.round(dm.widthPixels * 0.92f), dp(a, 420));
        a.getWindow().setLayout(w, ViewGroup.LayoutParams.WRAP_CONTENT);
    }
}
