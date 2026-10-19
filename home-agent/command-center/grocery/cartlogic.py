"""Site-independent cart helpers: merge policies and sent-vs-returned diff."""
from __future__ import annotations

from typing import Dict, Iterable

MERGE_POLICIES = ("max", "sum", "replace-ours")


class CartError(RuntimeError):
    pass


def to_qty(v) -> float:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return 0.0


def merge(current: Dict[str, float], ours: Dict[str, float], policy: str = "max") -> Dict[str, float]:
    """Keep everything already in the cart and add ours.
    max (default)  qty = max(existing, ours)  -> idempotent, re-running never doubles the cart.
    sum            qty = existing + ours.
    replace-ours   ours wins for our ids; other existing items untouched."""
    if policy not in MERGE_POLICIES:
        raise CartError(f"unknown merge policy {policy!r}; use one of {MERGE_POLICIES}")
    out = {str(k): float(v) for k, v in current.items()}
    for k, v in ours.items():
        k, v = str(k), float(v)
        if k in out:
            out[k] = max(out[k], v) if policy == "max" else (out[k] + v if policy == "sum" else v)
        else:
            out[k] = v
    return out


def diff(sent: Dict[str, float], returned: Dict[str, float], system_ids: Iterable[str] = ()) -> dict:
    system_ids = set(system_ids)
    dropped = sorted(set(sent) - set(returned))
    extra = sorted(k for k in returned if k not in sent)
    qty_changed = {k: (sent[k], returned[k]) for k in sent
                   if k in returned and returned[k] and abs(returned[k] - sent[k]) > 1e-6}
    return {"dropped": dropped,
            "system_lines": [k for k in extra if k in system_ids or not system_ids],
            "unexpected": [k for k in extra if system_ids and k not in system_ids],
            "qty_changed": qty_changed,
            "accepted": sorted(k for k in sent if k in returned)}


def format_result(d: dict) -> str:
    lines = [f"[{d.get('site')}] עגלה עודכנה: {len(d['accepted'])}/{d['sent_count']} מוצרים התקבלו."]
    if d["dropped"]:
        lines.append("⚠️ הוסרו ע\"י השרת (לא זמינים/לא מוכרים): " + ", ".join(d["dropped"]))
    if d["qty_changed"]:
        lines.append("⚠️ כמות שונתה: " + ", ".join(f"{k}: {a:g}→{b:g}" for k, (a, b) in d["qty_changed"].items()))
    if d.get("system_lines"):
        lines.append("שורות מערכת (משלוח וכו'): " + ", ".join(d["system_lines"]))
    if d.get("unexpected"):
        lines.append("שורות לא צפויות בעגלה: " + ", ".join(d["unexpected"]))
    if d.get("subtotal") is not None:
        lines.append(f"סכום ביניים לפי השרת: ₪{d['subtotal']}")
    lines.append(f"פתח/רענן את דף הקופה (התשלום ידני בלבד, לא מבוצע checkout): {d['checkout_url']}")
    return "\n".join(lines)
