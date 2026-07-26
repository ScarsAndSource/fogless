"""
Natural-language expense/income parser using Groq's free-tier LLM API.
Turns something like "400rs creatine cash" or "60rs milk upi" into a
structured transaction dict — no /add syntax needed.
"""
import os
import json
import requests


GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GROQ_API = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"  # fast + free-tier friendly; swap here if Groq deprecates it


EXPENSE_CATEGORIES = [
    "food", "groceries", "transport", "bills", "shopping", "entertainment",
    "health", "subscriptions", "rent", "education", "travel", "fitness", "other",
]
INCOME_CATEGORIES = ["salary", "freelance", "gift", "refund", "other"]
PAYMENT_METHODS = ["cash", "upi", "card", "netbanking", "other"]


SYSTEM_PROMPT = f"""You are a strict JSON-extraction engine for a personal expense tracker.
Given a short, casual message someone typed on their phone, extract a single transaction.


Expense categories (pick exactly one): {", ".join(EXPENSE_CATEGORIES)}
Income categories (pick exactly one): {", ".join(INCOME_CATEGORIES)}
Payment methods (pick exactly one if mentioned or clearly implied, else null): {", ".join(PAYMENT_METHODS)}


Rules:
- "type" is "expense" or "income". Assume "expense" unless words like salary, got paid, received, refund, credited clearly signal income.
- "amount" is a plain number (no currency symbols, no commas).
- "category" is exactly one value from the matching list above — pick the closest fit, never invent new categories.
- "payment_method" is exactly one of the listed methods, or null if not mentioned.
- "note" is the short specific detail (e.g. the item/person/service), or null if there isn't one.
- If the message doesn't describe money changing hands at all (a question, greeting, random text), respond with {{"type": null}}.


Respond with ONLY a raw JSON object matching this exact shape, nothing else — no markdown fences, no commentary:
{{"type": "expense", "amount": 0, "category": "other", "payment_method": null, "note": null}}
"""




def parse(text: str) -> dict | None:
    """Returns a parsed transaction dict, or None if it couldn't be parsed / isn't a transaction."""
    try:
        r = requests.post(
            GROQ_API,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": GROQ_MODEL,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
            },
            timeout=15,
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        data = json.loads(content)
    except Exception:
        return None


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
