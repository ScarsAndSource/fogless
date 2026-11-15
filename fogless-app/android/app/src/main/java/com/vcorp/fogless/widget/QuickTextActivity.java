package com.vcorp.fogless.widget;

import android.app.Activity;
import android.content.Context;
import android.graphics.Typeface;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputFilter;
import android.text.InputType;
import android.text.method.ScrollingMovementMethod;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.inputmethod.EditorInfo;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONObject;

/** Floating "type an expense" box opened from the capture widget. Posts to /api/message natively. */
public class QuickTextActivity extends Activity {
    private static final int MAX_LEN = 1000; // same as MAX_TEXT_LEN on the server

    private final Handler main = new Handler(Looper.getMainLooper());
    private EditText input;
    private TextView status;
    private Button send;
    private Button cancel;
    private boolean busy;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        Ui.transparentWindow(this);

        LinearLayout root = Ui.card(this);
        root.addView(Ui.label(this, "QUICK LOG", 12, Ui.MUTED, true));

        TextView hint = Ui.label(this, "24 coffee upi  \u00B7  got 424 upi  \u00B7  120 lunch, 50 bus cash", 11, Ui.MUTED, false);
        LinearLayout.LayoutParams hp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        hp.topMargin = Ui.dp(this, 4);
        root.addView(hint, hp);

        input = new EditText(this);
        input.setHint("What did you spend or receive?");
        input.setHintTextColor(0x99716450);
        input.setTextColor(Ui.INK);
        input.setTextSize(15);
        input.setTypeface(Typeface.MONOSPACE);
        input.setInputType(InputType.TYPE_CLASS_TEXT);
        input.setImeOptions(EditorInfo.IME_ACTION_SEND);
        input.setFilters(new InputFilter[]{new InputFilter.LengthFilter(MAX_LEN)});
        input.setBackground(Ui.rounded(this, 0xFFFFFFFF, Ui.BORDER, 12));
        input.setPadding(Ui.dp(this, 14), Ui.dp(this, 12), Ui.dp(this, 14), Ui.dp(this, 12));
        input.setOnEditorActionListener(new TextView.OnEditorActionListener() {
            @Override
            public boolean onEditorAction(TextView v, int actionId, KeyEvent event) {
                boolean enter = event != null && event.getKeyCode() == KeyEvent.KEYCODE_ENTER
                        && event.getAction() == KeyEvent.ACTION_DOWN;
                if (actionId == EditorInfo.IME_ACTION_SEND || enter) {
                    send();
                    return true;
                }
                return false;
            }
        });
        LinearLayout.LayoutParams ip = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        ip.topMargin = Ui.dp(this, 12);
        root.addView(input, ip);

        status = Ui.label(this, "", 12, Ui.MUTED, false);
        status.setVisibility(View.GONE);
        status.setMaxLines(10);
        status.setMovementMethod(new ScrollingMovementMethod());
        LinearLayout.LayoutParams stp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        stp.topMargin = Ui.dp(this, 10);
        root.addView(status, stp);

        LinearLayout buttons = new LinearLayout(this);
        buttons.setOrientation(LinearLayout.HORIZONTAL);
        buttons.setGravity(Gravity.END);
        cancel = Ui.button(this, "Cancel", false);
        cancel.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                finish();
            }
        });
        send = Ui.button(this, "Log it", true);
        send.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                send();
            }
        });
        buttons.addView(cancel);
        LinearLayout.LayoutParams sendLp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        sendLp.leftMargin = Ui.dp(this, 10);
        buttons.addView(send, sendLp);
        LinearLayout.LayoutParams bp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        bp.topMargin = Ui.dp(this, 14);
        root.addView(buttons, bp);

        setContentView(root);
        Ui.sizeDialog(this);
        setFinishOnTouchOutside(true);
        input.requestFocus();
    }

    private boolean alive() {
        return !isFinishing() && !isDestroyed();
    }

    private void setBusy(boolean b, String message, int color) {
        busy = b;
        input.setEnabled(!b);
        send.setEnabled(!b);
        send.setAlpha(b ? 0.5f : 1f);
        setFinishOnTouchOutside(!b);
        if (message == null || message.isEmpty()) {
            status.setVisibility(View.GONE);
        } else {
            status.setText(message);
            status.setTextColor(color);
            status.setVisibility(View.VISIBLE);
        }
    }

    private void send() {
        if (busy) return;
        final String text = input.getText().toString().trim();
        if (text.isEmpty()) {
            setBusy(false, "Type something first.", Ui.RED);
            return;
        }
        setBusy(true, "Logging\u2026", Ui.MUTED);

        final Context app = getApplicationContext();
        new Thread(new Runnable() {
            @Override
            public void run() {
                FoglessApi.ApiResponse r;
                try {
                    JSONObject body = new JSONObject();
                    body.put("text", text);
                    r = FoglessApi.postJson(app, "/api/message", body);
                } catch (Exception e) {
                    r = FoglessApi.ApiResponse.failure("Couldn't build the request.");
                }
                final FoglessApi.ApiResponse res = r;
                if (res.ok()) WidgetRefresher.requestNow(app);
                main.post(new Runnable() {
                    @Override
                    public void run() {
                        onResult(app, res);
                    }
                });
            }
        }).start();
    }

    private void onResult(Context app, FoglessApi.ApiResponse res) {
        String msg = res.message();
        if (res.ok()) {
            if (msg.length() <= 160 || !alive()) {
                Toast.makeText(app, msg, Toast.LENGTH_LONG).show();
                if (alive()) finish();
            } else {
                // long reply (e.g. a /stats or /history command): keep it readable on screen
                input.setText("");
                setBusy(false, msg, Ui.INK);
                cancel.setText("Close");
            }
        } else if (alive()) {
            // keep what they typed, so nothing is lost
            setBusy(false, msg, Ui.RED);
        } else {
            Toast.makeText(app, msg, Toast.LENGTH_LONG).show();
        }
    }
}
