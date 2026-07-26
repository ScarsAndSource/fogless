"""
Natural-language expense/income parsing.

Two paths, in order:
1. quick_parse_single — pure regex, zero API calls. Only fires for a single,
   simple item whose "note" has already been taught via /alias or an inline
   correction button. Instant, free, and never miscategorizes.
2. Groq (chat completions) — the general-purpose fallback. Also handles
   multi-item messages ("400 creatine cash, 60 milk upi") by returning a list.

Voice notes go through transcribe_voice() first, then the transcribed text
runs through the same parse_multi() pipeline above.
"""
import os
import re
import json
import requests


GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GROQ_CHAT_API = "https://api.groq.com/openai/v1/chat/completions"
GROQ_AUDIO_API = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "llama-3.3-70b-versatile"       # swap here if Groq deprecates it
GROQ_WHISPER_MODEL = "whisper-large-v3-turbo"  # swap to "whisper-large-v3" for max accuracy over speed


EXPENSE_CATEGORIES = [
    "food", "groceries", "transport", "bills", "shopping", "entertainment",
    "health", "subscriptions", "rent", "education", "travel", "fitness", "other",
]
INCOME_CATEGORIES = ["salary", "freelance", "gift", "refund", "other"]
PAYMENT_METHODS = ["cash", "upi", "card", "netbanking", "other"]


_CURRENCY_WORDS = {"rs", "rs.", "inr", "rupees", "rupee", "bucks"}


MULTI_SYSTEM_PROMPT = f"""You are a strict JSON-extraction engine for a personal expense tracker.
A message may describe ONE or SEVERAL transactions (separated by commas, "and", semicolons, or line breaks).
Extract every transaction you find.

Expense categories (pick exactly one per transaction): {", ".join(EXPENSE_CATEGORIES)}
Income categories (pick exactly one per transaction): {", ".join(INCOME_CATEGORIES)}
Payment methods (pick exactly one if mentioned or clearly implied, else null): {", ".join(PAYMENT_METHODS)}

Rules per transaction:
- "type" is "expense" or "income". Assume "expense" unless words like salary, got paid, received, refund, credited clearly signal income.
- "amount" is a plain number (no currency symbols, no commas). "1.5k" or "2k" means 1500 / 2000.
- "category" is exactly one value from the matching list above — pick the closest fit, never invent new categories.
- "payment_method" is exactly one of the listed methods, or null if not mentioned.
- "note" is the short specific detail (e.g. the item/person/service), or null if there isn't one.

If NOTHING in the message describes money changing hands, respond with {{"transactions": []}}.

Respond with ONLY a raw JSON object matching this exact shape, nothing else — no markdown fences, no commentary:
{{"transactions": [{{"type": "expense", "amount": 0, "category": "other", "payment_method": null, "note": null}}]}}
"""


_MULTI_HINT_RE = re.compile(r",|;|\band\b|\n", re.IGNORECASE)


def _validate_item(data: dict) -> dict | None:
    if not data or data.get("type") not in ("expense", "income"):
        return None
    try:
        amount = float(data["amount"])
    except (TypeError, ValueError, KeyError):
        return None
    if amount <= 0:
        return None
    valid_categories = EXPENSE_CATEGORIES if data["type"] == "expense" else INCOME_CATEGORIES
    category = data.get("category") if data.get("category") in valid_categories else "other"
    payment_method = data.get("payment_method") if data.get("payment_method") in PAYMENT_METHODS else None
    note = data.get("note") or None
    return {
        "type": data["type"],
        "amount": amount,
        "category": category,
        "payment_method": payment_method,
        "note": note,
    }


def _call_groq_chat(text: str) -> list[dict] | None:
    try:
        r = requests.post(
            GROQ_CHAT_API,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": GROQ_MODEL,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": MULTI_SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
            },
            timeout=20,
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        data = json.loads(content)
    except Exception:
        return None

    raw_items = data.get("transactions") if isinstance(data, dict) else None
    if not raw_items:
        return None

    results = [item for item in (_validate_item(x) for x in raw_items) if item is not None]
    return results or None


def quick_parse_single(text: str, aliases: dict) -> dict | None:
    """Regex-only fast path. Only returns a result if every remaining word
    after stripping the amount/currency/payment-method tokens matches a
    note that's already been taught (via /alias or a correction button)."""
    tokens = text.lower().replace("₹", " ").replace(",", " ").split()
    if not tokens:
        return None

    amount = None
    payment_method = None
    note_tokens = []

    for tok in tokens:
        clean = tok.strip(".,")
        if amount is None:
            m = re.match(r"^(\d+(?:\.\d+)?)(k)?$", clean)
            if m:
                val = float(m.group(1))
                if m.group(2) == "k":
                    val *= 1000
                amount = val
                continue
            m2 = re.match(r"^(\d+(?:\.\d+)?)rs$", clean)
            if m2:
                amount = float(m2.group(1))
                continue
        if clean in _CURRENCY_WORDS:
            continue
        if clean in PAYMENT_METHODS:
            payment_method = clean
            continue
        note_tokens.append(clean)

    if amount is None or amount <= 0 or not note_tokens:
        return None

    note_key = " ".join(note_tokens).strip()
    alias = aliases.get(note_key)
    if not alias:
        return None  # unknown item -> fall back to Groq, don't guess

    return {
        "type": alias["type"],
        "amount": amount,
        "category": alias["category"],
        "payment_method": payment_method or alias.get("payment_method"),
        "note": note_key,
    }


def parse_multi(text: str, aliases: dict) -> list[dict] | None:
    """Main entry point. Returns a list of transaction dicts, or None."""
    if not _MULTI_HINT_RE.search(text):
        quick = quick_parse_single(text, aliases)
        if quick:
            return [quick]
    return _call_groq_chat(text)


def transcribe_voice(audio_bytes: bytes, filename: str = "voice.ogg") -> str | None:
    try:
        r = requests.post(
            GROQ_AUDIO_API,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            files={"file": (filename, audio_bytes, "audio/ogg")},
            data={"model": GROQ_WHISPER_MODEL, "response_format": "text"},
            timeout=30,
        )
        r.raise_for_status()
        text = r.text.strip()
        return text or None
    except Exception:
        return None
