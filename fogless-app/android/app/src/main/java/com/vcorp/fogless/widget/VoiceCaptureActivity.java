package com.vcorp.fogless.widget;

import android.Manifest;
import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.media.MediaRecorder;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.provider.Settings;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.util.Locale;

/** Floating voice recorder opened from the capture widget: records m4a, uploads to /api/voice (Groq Whisper). */
public class VoiceCaptureActivity extends Activity {
    private static final int REQ_MIC = 71;
    private static final int MAX_MS = 30000;
    private static final int MIN_MS = 800;
    private static final int IDLE = 0;
    private static final int REC = 1;
    private static final int SENDING = 2;

    private final Handler main = new Handler(Looper.getMainLooper());
    private TextView timerView;
    private TextView statusView;
    private Button primary;
    private Button cancel;

    private MediaRecorder recorder;
    private File file;
    private long startedAt;
    private int state = IDLE;
    private boolean micDenied;

    private final Runnable tick = new Runnable() {
        @Override
        public void run() {
            if (state != REC) return;
            long s = (SystemClock.elapsedRealtime() - startedAt) / 1000;
            timerView.setText(String.format(Locale.US, "%d:%02d / 0:30", s / 60, s % 60));
            main.postDelayed(this, 250);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        Ui.transparentWindow(this);

        LinearLayout root = Ui.card(this);
        root.addView(Ui.label(this, "VOICE LOG", 12, Ui.MUTED, true));

        timerView = Ui.label(this, "0:00 / 0:30", 30, Ui.INK, true);
        timerView.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams tp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        tp.topMargin = Ui.dp(this, 14);
        root.addView(timerView, tp);

        statusView = Ui.label(this, "", 12, Ui.MUTED, false);
        statusView.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams sp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        sp.topMargin = Ui.dp(this, 8);
        root.addView(statusView, sp);

        LinearLayout buttons = new LinearLayout(this);
        buttons.setOrientation(LinearLayout.HORIZONTAL);
        buttons.setGravity(Gravity.END);
        cancel = Ui.button(this, "Cancel", false);
        cancel.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                cancelRecording();
                finish();
            }
        });
        primary = Ui.button(this, "Record", true);
        primary.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                onPrimary();
            }
        });
        buttons.addView(cancel);
        LinearLayout.LayoutParams pl = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        pl.leftMargin = Ui.dp(this, 10);
        buttons.addView(primary, pl);
        LinearLayout.LayoutParams bp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        bp.topMargin = Ui.dp(this, 16);
        root.addView(buttons, bp);

        setContentView(root);
        Ui.sizeDialog(this);
        setFinishOnTouchOutside(false); // a stray tap must not throw away a recording
        render(null, Ui.MUTED);

        beginFlow(); // they tapped the mic: start right away
    }

    // ------------------------------------------------------------------ flow

    private void onPrimary() {
        if (state == REC) {
            stopAndSend();
        } else if (state == IDLE) {
            if (micDenied && checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                Intent i = new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:" + getPackageName()));
                i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                try {
                    startActivity(i);
                } catch (RuntimeException ignored) {
                    // no settings screen available
                }
                return;
            }
            beginFlow();
        }
    }

    private void beginFlow() {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
            startRecording();
        } else {
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, REQ_MIC);
        }
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode != REQ_MIC) return;
        if (grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
            micDenied = false;
            startRecording();
        } else {
            micDenied = true;
            state = IDLE;
            render("Microphone is blocked. Allow it for Fogless in Android settings.", Ui.RED);
        }
    }

    @SuppressWarnings("deprecation")
    private void startRecording() {
        if (state != IDLE) return;
        MediaRecorder r = null;
        try {
            File f = new File(getCacheDir(), "widget_voice.m4a");
            if (f.exists()) f.delete();
            r = Build.VERSION.SDK_INT >= 31 ? new MediaRecorder(this) : new MediaRecorder();
            r.setAudioSource(MediaRecorder.AudioSource.MIC);
            r.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4);
            r.setAudioEncoder(MediaRecorder.AudioEncoder.AAC);
            r.setAudioChannels(1);
            r.setAudioSamplingRate(44100);
            r.setAudioEncodingBitRate(64000);
            r.setMaxDuration(MAX_MS);
            r.setOutputFile(f.getAbsolutePath());
            r.setOnInfoListener(new MediaRecorder.OnInfoListener() {
                @Override
                public void onInfo(MediaRecorder mr, int what, int extra) {
                    if (what == MediaRecorder.MEDIA_RECORDER_INFO_MAX_DURATION_REACHED) stopAndSend();
                }
            });
            r.prepare();
            r.start();
            recorder = r;
            file = f;
            startedAt = SystemClock.elapsedRealtime();
            state = REC;
            render("Speak your expense, then tap Stop.", Ui.MUTED);
            main.post(tick);
        } catch (Exception e) {
            if (r != null) {
                try {
                    r.release();
                } catch (Exception ignored) {
                    // already unusable
                }
            }
            recorder = null;
            state = IDLE;
            render("Couldn't start the microphone. Is another app using it?", Ui.RED);
        }
    }

    private void stopAndSend() {
        if (state != REC) return;
        long elapsed = SystemClock.elapsedRealtime() - startedAt;
        boolean valid = true;
        try {
            recorder.stop();
        } catch (RuntimeException e) {
            valid = false; // stopped before any audio arrived
        }
        releaseRecorder();
        main.removeCallbacks(tick);

        final File f = file;
        file = null;
        if (!valid || elapsed < MIN_MS || f == null || !f.exists() || f.length() < 1024) {
            if (f != null) f.delete();
            state = IDLE;
            timerView.setText("0:00 / 0:30");
            render("Too short, nothing was logged. Tap Record to try again.", Ui.RED);
            return;
        }

        state = SENDING;
        render("Transcribing and logging\u2026", Ui.MUTED);
        final Context app = getApplicationContext();
        new Thread(new Runnable() {
            @Override
            public void run() {
                byte[] data = readFile(f);
                f.delete();
                FoglessApi.ApiResponse r;
                if (data == null || data.length == 0) {
                    r = FoglessApi.ApiResponse.failure("Couldn't read the recording.");
                } else {
                    r = FoglessApi.postMultipart(app, "/api/voice", "audio", "voice.m4a", "audio/mp4", data);
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
        boolean alive = !isFinishing() && !isDestroyed();
        if (res.ok()) {
            String heard = Fmt.str(res.json, "transcribed");
            String msg = res.message();
            String shown = heard.isEmpty() ? msg : "\uD83C\uDF99 \u201C" + heard + "\u201D\n" + msg;
            if (shown.length() > 220) shown = shown.substring(0, 217) + "\u2026";
            Toast.makeText(app, shown, Toast.LENGTH_LONG).show();
            if (alive) finish();
        } else if (alive) {
            state = IDLE;
            timerView.setText("0:00 / 0:30");
            render(res.message(), Ui.RED);
        } else {
            Toast.makeText(app, res.message(), Toast.LENGTH_LONG).show();
        }
    }

    // ------------------------------------------------------------------ UI + cleanup

    private void render(String message, int color) {
        statusView.setText(message == null ? "" : message);
        statusView.setTextColor(color);
        statusView.setVisibility(message == null || message.isEmpty() ? View.GONE : View.VISIBLE);
        if (state == REC) {
            primary.setText("Stop & log");
            primary.setEnabled(true);
        } else if (state == SENDING) {
            primary.setText("Logging\u2026");
            primary.setEnabled(false);
        } else {
            primary.setText(micDenied ? "Open settings" : "Record");
            primary.setEnabled(true);
        }
        primary.setAlpha(primary.isEnabled() ? 1f : 0.5f);
    }

    private void releaseRecorder() {
        if (recorder != null) {
            try {
                recorder.release();
            } catch (Exception ignored) {
                // nothing else to do
            }
            recorder = null;
        }
    }

    /** Stop and throw the audio away. Safe to call in any state. */
    private void cancelRecording() {
        main.removeCallbacks(tick);
        if (state == REC && recorder != null) {
            try {
                recorder.stop();
            } catch (RuntimeException ignored) {
                // nothing recorded yet
            }
        }
        releaseRecorder();
        if (state == REC && file != null) file.delete();
        if (state == REC) {
            file = null;
            state = IDLE;
        }
    }

    @Override
    protected void onStop() {
        // Leaving the screen while recording would mean a hidden recording: drop it instead.
        if (state == REC) {
            cancelRecording();
            Toast.makeText(getApplicationContext(), "Recording cancelled.", Toast.LENGTH_SHORT).show();
            finish();
        }
        super.onStop();
    }

    @Override
    protected void onDestroy() {
        main.removeCallbacks(tick);
        releaseRecorder();
        super.onDestroy();
    }

    private static byte[] readFile(File f) {
        try (InputStream in = new FileInputStream(f)) {
            ByteArrayOutputStream bo = new ByteArrayOutputStream((int) Math.max(1024, f.length()));
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) != -1) bo.write(buf, 0, n);
            return bo.toByteArray();
        } catch (IOException e) {
            return null;
        }
    }
}
