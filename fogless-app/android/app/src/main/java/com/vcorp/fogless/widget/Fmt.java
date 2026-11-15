package com.vcorp.fogless.widget;

import org.json.JSONObject;

import java.text.NumberFormat;
import java.text.SimpleDateFormat;
import java.util.Calendar;
import java.util.Date;
import java.util.Locale;

/** Formatting + safe JSON helpers shared by the widgets. */
final class Fmt {
    static final String DOTS = "\u2022\u2022\u2022\u2022";

    private Fmt() {}

    /** String field or "" -- never the literal "null" that org.json returns for JSON nulls. */
    static String str(JSONObject o, String key) {
        if (o == null || o.isNull(key)) return "";
        return o.optString(key, "").trim();
    }

    /** "₹1,23,456.50" (absolute value, no sign). Whole numbers drop the decimals. */
    static String rupees(double v) {
        double a = Math.abs(v);
        boolean whole = Math.abs(a - Math.rint(a)) < 0.005;
        NumberFormat nf = NumberFormat.getNumberInstance(Locale.forLanguageTag("en-IN"));
        nf.setMinimumFractionDigits(whole ? 0 : 2);
        nf.setMaximumFractionDigits(whole ? 0 : 2);
        return "\u20B9" + nf.format(a);
    }

    /** Signed: negative values get a real minus sign. */
    static String money(double v) {
        return (v < -0.004 ? "\u2212" : "") + rupees(v);
    }

    static String cap(String s) {
        if (s == null || s.isEmpty()) return "";
        return s.substring(0, 1).toUpperCase(Locale.ROOT) + s.substring(1);
    }

    /** upi -> UPI, cash -> Cash, anything else capitalised. */
    static String pay(String pm) {
        if (pm == null || pm.isEmpty()) return "";
        if (pm.equalsIgnoreCase("upi")) return "UPI";
        return cap(pm);
    }

    /** "14:32" today, "9 Nov 14:32" otherwise. */
    static String stamp(long ms) {
        Calendar now = Calendar.getInstance();
        Calendar then = Calendar.getInstance();
        then.setTimeInMillis(ms);
        boolean sameDay = now.get(Calendar.YEAR) == then.get(Calendar.YEAR)
                && now.get(Calendar.DAY_OF_YEAR) == then.get(Calendar.DAY_OF_YEAR);
        return new SimpleDateFormat(sameDay ? "HH:mm" : "d MMM HH:mm", Locale.getDefault()).format(new Date(ms));
    }
}
