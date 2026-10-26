"""E.164 number handling.

PSTN destinations, DID inventory and caller-number matching all speak E.164
(``+`` + country calling code + national number, ≤ 15 digits). This module is the
single place that turns "what the user typed" into canonical E.164 and back.

See ``docs/design/PSTN-AND-DIDS.md``.
"""
from __future__ import annotations

import re

# Country calling codes we can resolve by prefix (longest match wins, so multi
# digit codes are matched before their single-digit parents).
CALLING_CODES = {
    "1": "US/CA", "7": "RU/KZ", "20": "EG", "27": "ZA", "30": "GR", "31": "NL",
    "32": "BE", "33": "FR", "34": "ES", "36": "HU", "39": "IT", "40": "RO",
    "41": "CH", "43": "AT", "44": "GB", "45": "DK", "46": "SE", "47": "NO",
    "48": "PL", "49": "DE", "51": "PE", "52": "MX", "54": "AR", "55": "BR",
    "56": "CL", "57": "CO", "60": "MY", "61": "AU", "62": "ID", "63": "PH",
    "64": "NZ", "65": "SG", "66": "TH", "81": "JP", "82": "KR", "84": "VN",
    "86": "CN", "90": "TR", "91": "IN", "92": "PK", "94": "LK", "95": "MM",
    "98": "IR", "212": "MA", "234": "NG", "254": "KE", "351": "PT", "353": "IE",
    "358": "FI", "380": "UA", "420": "CZ", "421": "SK", "880": "BD", "971": "AE",
    "972": "IL", "966": "SA", "974": "QA", "968": "OM", "965": "KW", "973": "BH",
}

# Default national calling code per ISO-ish country key, for parsing local
# numbers that arrive without a ``+``.
DEFAULT_COUNTRY_CODE = {
    "US": "1", "CA": "1", "GB": "44", "IE": "353", "IN": "91", "LK": "94",
    "PK": "92", "BD": "880", "AE": "971", "SA": "966", "DE": "49", "FR": "33",
    "NL": "31", "ES": "34", "IT": "39", "AU": "61", "NZ": "64", "SG": "65",
    "JP": "81", "BR": "55", "ZA": "27", "KE": "254", "NG": "234",
}

_STRIP = re.compile(r"[\s\-()./]")
_E164 = re.compile(r"^\+[1-9]\d{6,14}$")
_EXT = re.compile(r"[;,]?\s*(?:ext\.?|x|#)\s*[=:]?\s*(\d{1,6})$", re.IGNORECASE)


class NumberError(ValueError):
    """Raised when a number cannot be normalised to E.164."""


def _digits(number: str) -> tuple[str, str | None]:
    """Return ``(dialable_digits, extension)`` with separators removed."""
    raw = (number or "").strip()
    if raw.lower().startswith("tel:"):
        raw = raw[4:]
    ext = None
    m = _EXT.search(raw)
    if m:
        ext, raw = m.group(1), raw[: m.start()]
    return _STRIP.sub("", raw), ext


def normalize(number: str, default_country: str = "US") -> str:
    """Return canonical E.164 for ``number``.

    Accepted forms:

    * international — ``+1 555 123 4567`` or ``00 44 20 7946 0958``
    * national in ``default_country`` — ``(020) 7946 0958`` (trunk ``0`` dropped
      for non-NANP countries), or ``555 123 4567``
    * already carrying the country code without ``+`` — ``1 555 123 4567``
    * a trailing extension (``… x123``), which is ignored

    Raises :class:`NumberError` when the result is not valid E.164.
    """
    digits, _ext = _digits(number)
    if not digits:
        raise NumberError("empty number")
    if digits.startswith("+"):
        national = digits[1:]
    elif digits.startswith("00"):                    # international access prefix
        national = digits[2:]
    else:
        cc = DEFAULT_COUNTRY_CODE.get((default_country or "US").upper())
        if not cc:
            raise NumberError(f"unknown default country {default_country!r}")
        if digits.startswith(cc) and len(digits) - len(cc) >= 7:
            national = digits                        # already in country-code form
        else:
            if digits.startswith("0") and cc != "1":  # drop national trunk prefix
                digits = digits.lstrip("0")
            national = cc + digits
    if not national.isdigit():
        raise NumberError("number contains non-digits")
    e164 = "+" + national
    if not _E164.match(e164):
        raise NumberError(f"not a valid E.164 number: {e164!r}")
    return e164


def try_normalize(number: str, default_country: str = "US") -> str | None:
    """``normalize`` returning ``None`` instead of raising."""
    try:
        return normalize(number, default_country)
    except NumberError:
        return None


def is_e164(number: str) -> bool:
    return bool(_E164.match((number or "").strip()))


def validate(number: str, default_country: str = "US") -> dict:
    """``{"ok", "e164", "error"}`` — for API validate endpoints."""
    try:
        return {"ok": True, "e164": normalize(number, default_country), "error": None}
    except NumberError as exc:
        return {"ok": False, "e164": None, "error": str(exc)}


def calling_code(e164: str) -> str | None:
    """Country calling code (e.g. ``"+44"``) for an E.164 number, longest match."""
    if not is_e164(e164):
        return None
    body = e164[1:]
    for length in (3, 2, 1):
        if body[:length] in CALLING_CODES:
            return "+" + body[:length]
    return None


def region(e164: str) -> str | None:
    """Human region label for an E.164 number (best effort)."""
    cc = calling_code(e164)
    return CALLING_CODES.get(cc[1:]) if cc else None


def to_sip_uri(e164: str, host: str, *, transport: str | None = None,
               user: str = "phone") -> str:
    """SIP request-URI dialling ``e164`` through ``host`` (``user`` in the To)."""
    uri = f"sip:{e164}@{host}"
    if transport:
        uri += f";transport={transport}"
    return uri


def dial_user(e164: str, npa: bool = False) -> str:
    """The number as it appears in a SIP ``To`` user part (``+`` stripped)."""
    return e164.lstrip("+") if not npa else e164
