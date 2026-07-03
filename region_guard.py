#!/usr/bin/env python3
"""Defensive region guards for manual/one-off copyright alert posts."""

EXPECTED_REGIONS_BY_CHAT_ID = {
    "oc_e85373716ee746e3dc1bf999929cf1c4": {"US", "CA", "AU", "NZ"},
    "oc_04c1d1182d5795c182ca34dd152c5f91": {"MX", "CL", "CO", "AR", "ES", "PR", "PE"},
    "oc_6e157309d8d7145ba5ce7f0ba67354cb": {"BR"},
}


def normalize_region(value):
    return str(value or "").strip().upper()


def assert_region_allowed(chat_id, aeolus_row, *, upc=None, context="group post"):
    """Abort before posting if the Aeolus user_region does not match chat scope."""
    expected = EXPECTED_REGIONS_BY_CHAT_ID.get(chat_id)
    if not expected:
        return True

    actual = normalize_region((aeolus_row or {}).get("user_region"))
    if actual in expected:
        return True

    upc_text = upc or (aeolus_row or {}).get("upc") or "N/A"
    expected_text = ", ".join(sorted(expected))
    raise SystemExit(
        f"ERROR: Region guard aborted {context}: UPC {upc_text} has "
        f"user_region={actual or 'N/A'}, but chat_id={chat_id} only allows "
        f"{{{expected_text}}}. No group post was made."
    )
