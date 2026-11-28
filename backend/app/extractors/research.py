"""Structured multi-page research extractor for e-commerce, travel, and product comparison."""
from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Any, Optional

from ..website_resolver import KNOWN_DOMAINS_REGISTRY, website_resolver

# Task 8: the retailer table used to be a second hard-coded copy of the
# site registry. It is now DERIVED from the data file
# (backend/app/known_domains.json, category == "retail") — adding a
# retailer is a data edit, never a code change. A registry entry may
# carry an optional "display_name" (the label comparison tables show);
# otherwise the registry name is used.
def _retailers_from_registry() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for key, entry in KNOWN_DOMAINS_REGISTRY.items():
        if entry.get("category") != "retail":
            continue
        regional = entry.get("regional_urls") or {}
        out[key] = {
            "key": key,
            "name": entry.get("display_name") or entry["name"],
            "domains": list(entry["allowed_domains"]),
            # Default region is IN (same as the resolver); fall back to
            # the global URL so the derived table matches the old one.
            "default_url": (
                regional.get("IN")
                or regional.get("GLOBAL")
                or f"https://{entry['primary_domain']}"
            ),
            "patterns": list(entry["patterns"]),
        }
    return out


KNOWN_RETAILERS: dict[str, dict[str, Any]] = _retailers_from_registry()

# Retail display names (data) for stripping "on <site>" tails from a
# product query and " - <site>" suffixes from a product title. Built
# from the registry, never hand-listed.
_RETAILER_DISPLAY_NAMES = "|".join(
    sorted(
        (
            re.escape(entry.get("display_name") or entry["name"])
            for entry in KNOWN_DOMAINS_REGISTRY.values()
            if entry.get("category") == "retail"
        ),
        key=len,
        reverse=True,
    )
)

# A site token after "on / across / from / at": a registry retail name
# (case-insensitive, with an optional generic word after it), any other
# capitalised name, or a domain.
_SITE_AFTER_PREP = (
    r"(?:(?i:" + _RETAILER_DISPLAY_NAMES + r")(?:\s+(?:store|shop|official|website|portal|app))?"
    r"|[A-Z][A-Za-z0-9\-]*(?:\s+(?:[A-Z][A-Za-z0-9\-]*|store|shop|official|website|portal|app))?"
    r"|[a-z][a-z0-9\-]*(?:\.[a-z]{2,})+)"
)

# Words that may follow a site name in a comparison list ("Acme Store",
# "official store"). Anything else ends the site token.
_SITE_NAME_CONTINUATIONS = {"store", "shop", "official", "website", "portal", "app"}

_COMPARE_SITE_LIST_RE = re.compile(
    # Dots stay in the capture: a site token may be a domain
    # ("examplestore.com"). Sentence-ending periods are stripped per
    # token by _site_token_words; junk tokens are filtered by resolution.
    r"\b(?:compare|comparison)\b.*?\b(?:on|across|between)\s+([^;:!?]+)",
    re.IGNORECASE,
)


def _site_token_words(token: str) -> str:
    """Leading words of a comparison-list token that look like a site name.

    "ExampleStore for under 50000" -> "ExampleStore"; "Acme Store" ->
    "Acme Store"; "examplestore.com" -> "examplestore.com".
    """
    words: list[str] = []
    for raw in token.split():
        word = raw.strip(",.")
        if not word:
            continue
        if not words:
            if word[0].isupper() or "." in word:
                words.append(word)
            else:
                break
        elif word[0].isupper() or word.lower() in _SITE_NAME_CONTINUATIONS or "." in word:
            words.append(word)
        else:
            break
    return " ".join(words)


def _generic_required_sites(
    instruction: str, seen_keys: set[str], seen_domains: set[str]
) -> list[dict[str, Any]]:
    """Required sites the user named that are NOT in the retailer registry.

    Parses the site list of a comparison phrase ("compare X on A, B and C")
    and resolves each token through the website resolver. A token becomes
    a required site when it resolves to a known registry entry, a direct
    URL, or is itself a domain (``examplestore.com``) that passes the
    lookalike check. A bare unknown name resolves to a web-search
    fallback, which is not a site — the general planner path handles it
    instead.
    """
    from ..website_resolver import is_lookalike_or_suspicious

    match = _COMPARE_SITE_LIST_RE.search(instruction or "")
    if not match:
        return []
    found: list[dict[str, Any]] = []
    for chunk in re.split(r",|\band\b", match.group(1)):
        name = _site_token_words(chunk.strip().lstrip("the ").strip())
        if not name:
            continue
        key = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        if not key or key in seen_keys:
            continue
        try:
            resolved = website_resolver.resolve(name)
        except Exception:
            continue
        if resolved.resolution_method in ("registry", "direct_url"):
            if not resolved.primary_domain or not resolved.canonical_url:
                continue
            if resolved.primary_domain in seen_domains:
                continue
            seen_keys.add(key)
            seen_domains.add(resolved.primary_domain)
            found.append({
                "site_key": key,
                "name": resolved.target_name,
                "domains": [resolved.primary_domain],
                "default_url": resolved.canonical_url,
            })
        elif re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9\-]*(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}", name):
            domain = name.lower()
            if domain in seen_domains:
                continue
            is_risk, _ = is_lookalike_or_suspicious(domain)
            if is_risk:
                continue
            seen_keys.add(key)
            seen_domains.add(domain)
            found.append({
                "site_key": key,
                "name": name,
                "domains": [domain],
                "default_url": f"https://{domain}",
            })
    return found


def identify_required_sites(instruction: str) -> list[dict[str, Any]]:
    """Identify required websites/stores for multi-site comparison or research."""
    if not instruction:
        return []
    inst_lower = instruction.lower()
    found: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    for key, info in KNOWN_RETAILERS.items():
        matched = False
        for pat in info["patterns"]:
            if re.search(pat, inst_lower):
                matched = True
                break
        # Bare-name mention alongside another retailer: registry patterns
        # need a full name ("Acme Store") or a domain ("acme.com"),
        # but a bare name in a list ("across RetailerA, RetailerB,
        # Acme") is unambiguous once another retailer matched.
        if not matched:
            bare = info["name"].split()[0].lower()
            if re.search(r"(?:across|on|and|,)\s+(?:the\s+)?" + re.escape(bare) + r"\b", inst_lower):
                other_retailers = any(
                    k != key and any(re.search(p, inst_lower) for p in KNOWN_RETAILERS[k]["patterns"])
                    for k in KNOWN_RETAILERS
                )
                if other_retailers:
                    matched = True

        if matched and key not in seen_keys:
            seen_keys.add(key)
            found.append({
                "site_key": key,
                "name": info["name"],
                "domains": list(info["domains"]),
                "default_url": info["default_url"],
            })

    # Task 8: sites the user named that no registry covers (e.g. a
    # regional retailer, or a domain like examplestore.com) still become
    # required sites — resolved generically, not from a hard-coded list.
    seen_domains = {d for s in found for d in s["domains"]}
    found.extend(_generic_required_sites(instruction, seen_keys, seen_domains))

    return found


def is_multi_site_instruction(instruction: str) -> bool:
    """True if instruction explicitly asks for multi-site research or comparison."""
    if not instruction:
        return False
    sites = identify_required_sites(instruction)
    return len(sites) >= 2


def init_multi_site_tracker(
    instruction: str, existing_tracker: Optional[dict[str, Any]] = None
) -> Optional[dict[str, Any]]:
    """Initialize or update multi-site comparison tracker for multi-site tasks."""
    sites = identify_required_sites(instruction)
    if len(sites) < 2:
        return existing_tracker

    existing_sites = (existing_tracker or {}).get("required_sites", {})
    required_sites: dict[str, dict[str, Any]] = {}

    for s in sites:
        k = s["site_key"]
        if k in existing_sites:
            required_sites[k] = dict(existing_sites[k])
        else:
            required_sites[k] = {
                "site_key": k,
                "name": s["name"],
                "domains": s["domains"],
                "default_url": s["default_url"],
                "status": "pending",  # "pending", "in_progress", "completed", "unavailable"
                "data": None,
                "reason": None,
                "visited_urls": [],
            }

    return {
        "instruction": instruction,
        "required_sites": required_sites,
        "total_count": len(required_sites),
    }


def detect_site_unavailability(url: str, page_text: str, title: str) -> Optional[str]:
    """Detect if a product or listing is unavailable on the current page."""
    if not page_text and not title:
        return None
    full = f"{title}\n{page_text}".lower()
    unavail_patterns = [
        r"\bno results found\b",
        r"\bdid not match any products\b",
        r"\bwe couldn't find any matches\b",
        r"\b0 results for\b",
        r"\bcurrently unavailable\b",
        r"\bout of stock\b",
        r"\bpage not found\b",
        r"\bitem not available\b",
        r"\bproduct not available\b",
        r"\bwe couldn't find what you were looking for\b",
    ]
    for p in unavail_patterns:
        if re.search(p, full):
            return "No matching product found on this website"
    return None


def update_multi_site_tracker(
    tracker: dict[str, Any],
    url: str,
    title: str,
    page_text: str,
    collected_research: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Update required sites in tracker based on current page state and collected facts."""
    if not tracker or not tracker.get("required_sites"):
        return tracker

    coll = collected_research or {}
    domain = urllib.parse.urlparse(url).netloc.lower().replace("www.", "") if url else ""

    for site_key, site in tracker["required_sites"].items():
        # 1. Check if collected research has a fact matching this site's domains
        for d in site.get("domains", []):
            if d in coll:
                site["status"] = "completed"
                site["data"] = coll[d]
                site["reason"] = None
                break

        # 2. Check if current URL belongs to this site
        if any(d in domain for d in site.get("domains", [])):
            if url and url not in site.get("visited_urls", []):
                site.setdefault("visited_urls", []).append(url)
            # If not already completed, check if unavailable
            if site["status"] != "completed":
                unavail_reason = detect_site_unavailability(url, page_text, title)
                if unavail_reason:
                    site["status"] = "unavailable"
                    site["reason"] = unavail_reason
                elif site["status"] == "pending":
                    site["status"] = "in_progress"

    return tracker


def get_pending_sites(tracker: dict[str, Any]) -> list[dict[str, Any]]:
    """Return sites that have not yet been completed or marked unavailable."""
    if not tracker or not tracker.get("required_sites"):
        return []
    return [s for s in tracker["required_sites"].values() if s.get("status") in ("pending", "in_progress")]


def get_completed_sites(tracker: dict[str, Any]) -> list[dict[str, Any]]:
    """Return sites where product details and prices were successfully verified."""
    if not tracker or not tracker.get("required_sites"):
        return []
    return [s for s in tracker["required_sites"].values() if s.get("status") == "completed"]


def get_unavailable_sites(tracker: dict[str, Any]) -> list[dict[str, Any]]:
    """Return sites confirmed to be unavailable or with no matching listing."""
    if not tracker or not tracker.get("required_sites"):
        return []
    return [s for s in tracker["required_sites"].values() if s.get("status") == "unavailable"]


def is_multi_site_task_complete(tracker: dict[str, Any]) -> bool:
    """True only when every required site is either completed or confirmed unavailable."""
    if not tracker or not tracker.get("required_sites"):
        return True
    return len(get_pending_sites(tracker)) == 0 and len(tracker["required_sites"]) > 0


def is_already_resolved_site(target_url: str, tracker: dict[str, Any]) -> bool:
    """Check if target_url belongs to a site that was already completed or marked unavailable."""
    if not tracker or not tracker.get("required_sites") or not target_url:
        return False
    try:
        target_domain = urllib.parse.urlsplit(target_url).hostname or ""
        target_domain = target_domain.lower().replace("www.", "")
    except Exception:
        return False
    if not target_domain:
        return False
    for s in tracker["required_sites"].values():
        if s.get("status") in ("completed", "unavailable"):
            if any(target_domain == d or target_domain.endswith("." + d) or d.endswith("." + target_domain) for d in s.get("domains", [])):
                return True
    return False


def extract_target_product_query(instruction: str) -> str:
    """Extract core product search query from a comparison instruction."""
    text = (instruction or "").strip()
    # Strip leading phrases
    text = re.sub(r"^(?:find|search(?:\s+for)?|look\s+up|check\s+price(?:\s+of)?|compare(?:\s+prices\s+of)?)\s+", "", text, flags=re.IGNORECASE)
    # Strip trailing "and compare prices", "and compare", "compare prices"
    text = re.sub(r"\s+(?:and\s+)?compare(?:\s+prices)?.*$", "", text, flags=re.IGNORECASE)
    # Strip "on [site]...", "across [site]..." — the site token is any
    # registry retail name, capitalised name, or domain (Task 8: no
    # hard-coded store list).
    text = re.sub(r"\s+(?:on|across|from|at)\s+" + _SITE_AFTER_PREP + r".*$", "", text)
    return text.strip()


def claims_cheapest_on_single_listing(answer: str, completed_count: int) -> bool:
    """True if answer claims 'cheapest' or 'lowest price' when fewer than 2 listings were verified."""
    if completed_count >= 2:
        return False
    ans_lower = (answer or "").lower()
    return bool(re.search(r"\b(is\s+the\s+cheapest|cheapest\s+option\s+is|lowest\s+price\s+is\s+on|cheapest\s+price\s+is\s+on)\b", ans_lower))


def format_comparison_table(tracker: dict[str, Any], instruction: str = "") -> str:
    """Format markdown comparison table distinguishing verified, unavailable, and unvisited sites."""
    if not tracker or not tracker.get("required_sites"):
        return ""

    rows = []
    completed = get_completed_sites(tracker)
    valid_prices: list[tuple[float, str, str]] = []  # (price_val, site_name, price_str)

    for s in tracker["required_sites"].values():
        status = s.get("status", "pending")
        name = s["name"]
        if status == "completed" and s.get("data"):
            d = s["data"]
            price_str = d.get("price", "N/A")
            url = d.get("url", s.get("default_url", ""))
            variant = d.get("variant", "Standard")
            rows.append(f"| **{name}** | Verified | {price_str} | {variant} | [Link]({url}) |")
            nums = re.findall(r"[\d,]+(?:\.\d+)?", price_str.replace("₹", "").replace("$", ""))
            if nums:
                try:
                    val = float(nums[0].replace(",", ""))
                    valid_prices.append((val, name, price_str))
                except ValueError:
                    pass
        elif status == "unavailable":
            reason = s.get("reason") or "No matching listing found"
            url = s.get("default_url", "")
            rows.append(f"| **{name}** | Unavailable | N/A | {reason} | [Link]({url}) |")
        else:  # pending / unvisited
            url = s.get("default_url", "")
            rows.append(f"| **{name}** | Unvisited | N/A | Not visited | [Link]({url}) |")

    table_header = "| Retailer | Status | Price | Details | Link |\n| :--- | :--- | :--- | :--- | :--- |"
    table = f"{table_header}\n" + "\n".join(rows)

    # Product images, one per verified listing that declares one —
    # the same card-with-image shape other agents show, rendered
    # above the table. The image URL comes from the product page's
    # own og:image (validated in the extractor); the workspace
    # re-validates it before emitting an <img>.
    images = []
    for s in tracker["required_sites"].values():
        if s.get("status") == "completed" and s.get("data"):
            d = s["data"]
            img = d.get("image")
            if img:
                images.append(f"![{d.get('title', s['name'])} — {d.get('price', '')}]({img})")
    images_section = ("\n\n" + "\n\n".join(images)) if images else ""

    conclusion = ""
    if len(valid_prices) >= 2:
        valid_prices.sort(key=lambda x: x[0])
        cheapest = valid_prices[0]
        second = valid_prices[1]
        diff = second[0] - cheapest[0]
        if diff > 0:
            conclusion = f"\n\n**Cheapest Option**: **{cheapest[1]}** at {cheapest[2]} (₹{diff:,.0f} lower than {second[1]} at {second[2]})."
        else:
            conclusion = f"\n\n**Cheapest Option**: **{cheapest[1]}** and **{second[1]}** are tied at {cheapest[2]}."
    elif len(valid_prices) == 1:
        # Requirement 7: Never claim the cheapest option based on only one listing
        single = valid_prices[0]
        conclusion = (
            f"\n\n**Price Finding**: **{single[1]}** has the only verified listing at {single[2]}. "
            "A comparative cheapest option cannot be determined because the other retailers were unavailable or unvisited."
        )
    else:
        conclusion = "\n\n**Summary**: No verified listings were found across the requested retailers."

    return f"### Price & Availability Comparison:{images_section}\n{table}{conclusion}"


def _valid_image_url(raw: str | None) -> str | None:
    """Accept a page-declared product image URL, or None.

    Only credential-free http(s) URLs with a hostname pass. The host
    is deliberately NOT restricted to the page host — a product image
    almost always lives on the store's image CDN, not the listing
    page's own host. The URL is only ever rendered for a product page
    the agent actually visited, and the workspace re-validates it
    with safeHttpUrl before emitting an <img>.
    """
    if not raw or not isinstance(raw, str):
        return None
    try:
        parsed = urllib.parse.urlparse(raw.strip())
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    if not parsed.netloc or parsed.username or parsed.password:
        return None
    return raw.strip()[:2048]


# ---------------------------------------------------------------------------
# Price disambiguation (Stream 2, price + form safety).
#
# A product page routinely shows FIVE different currency amounts at
# once — the current (selling) price, the struck-through MRP /
# original price, the discount ("you save ₹X" / "50% off"), taxes /
# GST, and unrelated amounts (shipping, accessories, other listings).
# The old extractor took the FIRST currency match in the text, so
# whichever amount the page happened to print first — usually the
# MRP — became "the price". That is wrong by construction.
#
# disambiguate_prices() classifies EVERY amount by the label next to
# it (labels precede the amount: "MRP ₹9,999", "Selling price
# ₹4,199", "Save ₹5,800", "Shipping ₹99"; or trail it: "₹1,200
# off"). Windows are bounded by neighbouring amounts, so a label
# attaches to the amount it actually sits next to. The current price
# is then picked by precedence — an explicitly labelled current
# price, else a bare "Price:" label, else the single unambiguous
# amount on the page — and NEVER an MRP / tax / shipping / discount
# amount, and NEVER "whichever came first" when several unlabelled
# amounts compete: that case is honestly unknown (None).
# ---------------------------------------------------------------------------

# Currency-amount grammar (Stream A, issue 1).
#
# The old regex only understood Western 3-digit grouping
# (``[0-9]{1,3}(?:,[0-9]{3})*``) and, because regex alternation
# tries the first branch first, it TRUNCATED everything else:
# ``₹1,29,999`` matched as ``1`` (Indian grouping: first group 1–2
# digits, middle groups of 2, last group of 3), ungrouped
# ``₹89999`` matched as ``899`` (``[0-9]{1,3}`` won), and European
# ``€1.299,99`` matched as ``1.29``. A price of ₹1,29,999 became
# ₹1 — silently, with no error anywhere downstream.
#
# The regex now captures the whole numeric token (digits, dots and
# commas, ending in a digit) and :func:`_parse_price_amount`
# interprets the separators generically — no per-currency or
# per-site special cases:
#
# * both ``.`` and ``,`` present: the LAST separator is the decimal
#   one (``1,299.99`` → 1299.99; ``1.299,99`` → 1299.99);
# * commas only: thousands grouping, Indian (``1,29,999``,
#   ``12,34,567``) or Western (``129,999``) — validated, not
#   assumed (a malformed ``1,2,3`` is rejected, not guessed);
# * a lone dot with a 1–2 digit tail is a decimal (``4199.50``);
#   a lone dot with a 3-digit tail is a thousands separator only
#   for ``€``/``EUR`` (``€1.299`` = 1299) and a decimal otherwise;
# * a lone comma with a 2-digit tail is a decimal comma only for
#   ``€``/``EUR`` (``€12,99`` = 12.99); elsewhere it must be valid
#   grouping.
_AMOUNT_RE = re.compile(
    r"(₹|Rs\.?|INR|USD|EUR|GBP|\$|£|€)\s*([0-9](?:[0-9.,]*[0-9])?)",
    re.IGNORECASE,
)
_MAX_SANE_PRICE = 10_000_000.0

_SYMBOL_TO_CURRENCY = {
    "₹": "INR", "Rs": "INR", "Rs.": "INR", "INR": "INR",
    "$": "USD", "USD": "USD",
    "£": "GBP", "GBP": "GBP",
    "€": "EUR", "EUR": "EUR",
}
# Currencies whose everyday notation uses a comma as the decimal
# separator (and a dot for thousands). Used only to resolve the two
# genuinely ambiguous spellings above — never to invent a price.
_DECIMAL_COMMA_CURRENCIES = {"€", "EUR"}


def _normalize_currency(symbol: str | None) -> str | None:
    """Currency symbol/code → ISO code (``₹`` → ``INR``), else None."""
    if not symbol:
        return None
    key = symbol.strip()
    if key in _SYMBOL_TO_CURRENCY:
        return _SYMBOL_TO_CURRENCY[key]
    upper = key.upper().rstrip(".")
    return _SYMBOL_TO_CURRENCY.get(upper)


def _strip_grouping(int_part: str) -> str | None:
    """Validate a thousands-grouped integer part, return digits only.

    Accepts plain digits, Western grouping (``129,999`` — first
    group 1–3 digits, rest exactly 3) and Indian grouping
    (``1,29,999`` / ``12,34,567`` — first group 1–2 digits, middle
    groups exactly 2, last group exactly 3). The separator must be
    one character throughout. Returns None for malformed grouping.
    """
    if int_part.isdigit():
        return int_part
    sep = "," if "," in int_part else ("." if "." in int_part else None)
    if sep is None:
        return None
    if ("," in int_part) and ("." in int_part):
        return None  # mixed separators in the integer part
    groups = int_part.split(sep)
    if any(not g.isdigit() or not g for g in groups):
        return None
    if all(len(g) == 3 for g in groups[1:]) and 1 <= len(groups[0]) <= 3:
        return "".join(groups)  # Western
    if (1 <= len(groups[0]) <= 2 and len(groups[-1]) == 3
            and all(len(g) == 2 for g in groups[1:-1])):
        return "".join(groups)  # Indian
    return None


def _parse_grouped_amount(raw: str, symbol: str | None = None) -> float | None:
    """Parse a locale-formatted amount token (see _AMOUNT_RE notes).

    Returns None for malformed tokens instead of a truncated value.
    """
    if raw is None:
        return None
    s = str(raw).strip().replace("\u00a0", "").replace(" ", "")
    if not s or not s[0].isdigit() or not s[-1].isdigit():
        return None
    last_dot = s.rfind(".")
    last_comma = s.rfind(",")
    decimal_sep: str | None = None
    if last_dot >= 0 and last_comma >= 0:
        decimal_sep = "." if last_dot > last_comma else ","
    elif last_comma >= 0:
        if (s.count(",") == 1 and len(s) - last_comma - 1 == 2
                and (symbol or "").strip() in _DECIMAL_COMMA_CURRENCIES):
            decimal_sep = ","
    elif last_dot >= 0:
        tail = len(s) - last_dot - 1
        if tail in (1, 2):
            decimal_sep = "."
        elif tail == 3 and (symbol or "").strip() in _DECIMAL_COMMA_CURRENCIES:
            decimal_sep = None  # dot is the thousands separator
        elif tail == 3:
            decimal_sep = "."
    if decimal_sep:
        int_part, frac_part = s.rsplit(decimal_sep, 1)
        if not frac_part.isdigit() or not 1 <= len(frac_part) <= 3:
            return None
    else:
        int_part, frac_part = s, None
    digits = _strip_grouping(int_part)
    if digits is None:
        return None
    try:
        value = float(digits + ("." + frac_part if frac_part else ""))
    except ValueError:
        return None
    if not 0 < value <= _MAX_SANE_PRICE:
        return None
    return value


def _parse_price_amount(raw: str, symbol: str | None = None) -> float | None:
    """Parse one amount token (grouping-aware, see above)."""
    return _parse_grouped_amount(raw, symbol)

# Labels checked in the text immediately around an amount. Order is
# the classification priority: exclusion labels (original / discount
# / taxes / unrelated) are checked BEFORE current-price labels, so a
# related product's "Price: ₹2,499" under a "Related products"
# heading is classified as other, never as the current price.
_ORIGINAL_LABEL_RE = re.compile(
    r"\b(?:m\.?r\.?p\.?|list\s+price|original\s+price|regular\s+price|compare(?:d)?(?:\s+at)?|was)\b", re.I)
_DISCOUNT_LABEL_RE = re.compile(
    r"\b(?:you\s+)?(?:save|saved|savings?|discount)\b|\boff\b", re.I)
_TAX_LABEL_RE = re.compile(
    r"\b(?:tax|taxes|gst|vat|inclusive\s+of(?:\s+all)?\s+taxes?)\b", re.I)
_OTHER_LABEL_RE = re.compile(
    r"\b(?:shipping|delivery(?:\s+charge)?|handling|accessor(?:y|ies)|related|similar|"
    r"starting\s+at|from|other\s+sellers?|sponsored)\b", re.I)
_CURRENT_LABEL_RE = re.compile(
    r"\b(?:selling|current|offer|sale|special|deal|discounted)\s+price\b"
    r"|\bprice\s*:"
    r"|\bour\s+price\b"
    r"|\b(?:now|only)\b"
    r"|\bbuy\s+now\b|\badd\s+to\s+cart\b", re.I)
_FILTER_BOUND_RE = re.compile(r"\b(?:min|max)\b", re.I)
_PERCENT_OFF_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*off\b", re.I)
_BARE_PRICE_WORD_RE = re.compile(r"\bprice\b", re.I)


def _label_window(text: str, start: int, end: int,
                  prev_end: int, next_start: int | None) -> tuple[str, str]:
    """Text around one amount, bounded by its neighbouring amounts.

    ``before`` is at most 90 chars before the amount (but never
    earlier than the previous amount's end, so a label cannot leak
    across from another amount); ``after`` is at most 50 chars after
    it (never past the next amount's start).
    """
    before = text[max(prev_end, start - 90):start]
    after = text[end:min(next_start if next_start is not None else end + 50, end + 50)]
    return before, after


def _classified_amounts(flat: str, matches: list) -> list[dict[str, Any]]:
    """Every currency amount in flattened text, classified by its
    nearest label (``value`` / ``kind`` / ``symbol`` / ``near`` /
    ``start`` / ``end``). Shared by :func:`disambiguate_prices` and
    :func:`extract_variant_prices` so both classify the same amount
    the same way. ``kind`` is one of current / original / discount /
    taxes / other / filter-bound / unlabelled."""
    amounts: list[dict[str, Any]] = []
    for i, m in enumerate(matches):
        value = _parse_price_amount(m.group(2), m.group(1))
        if value is None:
            continue
        prev_end = matches[i - 1].end() if i else 0
        next_start = matches[i + 1].start() if i + 1 < len(matches) else None
        before, after = _label_window(flat, m.start(), m.end(), prev_end, next_start)
        # The label that classifies an amount is the NEAREST one:
        # the rightmost label in the text before it ("MRP ₹9,999"
        # vs "Selling Price ₹4,199" on adjacent lines must not
        # bleed into each other), or a label immediately after it
        # ("₹1,200 off", "₹756 tax included"). A trailing label
        # only counts when it starts the text after the amount —
        # "₹4,199 … 50% off" is the NEXT amount's discount, not
        # this amount's, so a bare "off" floating later in the
        # after-window must not steal the classification.
        near_before = before[-45:]
        near_after = after[:30]
        if _FILTER_BOUND_RE.search(near_before) and _BARE_PRICE_WORD_RE.search(before):
            kind = "filter-bound"
        else:
            candidates: list[tuple[int, int, str]] = []
            for label_re, label_kind in (
                (_ORIGINAL_LABEL_RE, "original"),
                (_DISCOUNT_LABEL_RE, "discount"),
                (_TAX_LABEL_RE, "taxes"),
                (_OTHER_LABEL_RE, "other"),
                (_CURRENT_LABEL_RE, "current"),
            ):
                found = list(label_re.finditer(near_before))
                if found:
                    last = found[-1]
                    candidates.append((len(near_before) - last.end(), 0, label_kind))
            for label_re, label_kind in (
                (re.compile(r"\s*((?:m\.?r\.?p\.?|list\s+price|original\s+price|regular\s+price))\b", re.I), "original"),
                (re.compile(r"\s*(off)\b", re.I), "discount"),
                (re.compile(r"\s*((?:tax|taxes|gst|vat|inclusive\s+of))\b", re.I), "taxes"),
                (re.compile(r"\s*((?:shipping|delivery(?:\s+charge)?|handling))\b", re.I), "other"),
            ):
                trailing = label_re.match(near_after)
                if trailing:
                    # A trailing label belongs to the NEXT amount, not
                    # this one, when another amount follows it almost
                    # immediately ("₹1,29,999 Shipping ₹99": "Shipping"
                    # labels ₹99, it must not steal ₹1,29,999 into
                    # "other"). The label's absolute end in the flat
                    # text is m.end() + trailing.end() — the windowed
                    # near_after is already cut at the next amount, so
                    # the check must look at the flat text itself.
                    label_end_abs = m.end() + trailing.end()
                    if _AMOUNT_RE.search(flat[label_end_abs:label_end_abs + 15]):
                        continue
                    # Distance is where the label WORD starts, not
                    # where the leading whitespace starts: "₹5,800
                    # Taxes" — "Taxes" is 1 char after the amount,
                    # the same distance as the "save" before it, so
                    # the before-label (distance tie, flag 0) wins.
                    candidates.append((trailing.start(1), 1, label_kind))
            if candidates:
                candidates.sort(key=lambda c: (c[0], c[1]))
                kind = candidates[0][2]
            else:
                kind = "unlabelled"
        amounts.append({"value": value, "kind": kind, "symbol": m.group(1),
                        "near": (near_before + " | " + near_after).strip(),
                        "start": m.start(), "end": m.end()})
    return amounts


def disambiguate_prices(text: str) -> dict[str, Any]:
    """Classify every currency amount on a page by its nearby label.

    Returns ``{"current", "original", "discount_amount",
    "discount_percent", "taxes", "other": [{label, value}], "currency",
    "current_source"}``. ``current`` is the selling price only — an
    MRP, a discount, a tax or a shipping amount is never returned as
    the current price, and when several unlabelled amounts compete
    with no labelled current price, ``current`` is None (ambiguous,
    honestly unknown) rather than the first match.
    """
    out: dict[str, Any] = {
        "current": None, "current_source": None, "original": None,
        "discount_amount": None, "discount_percent": None, "taxes": None,
        "other": [], "currency": None, "current_reason": None,
    }
    if not text:
        return out
    flat = re.sub(r"\s+", " ", text)
    matches = list(_AMOUNT_RE.finditer(flat))
    if not matches:
        out["current_reason"] = "no-amounts-on-page"
        return out
    percent = _PERCENT_OFF_RE.search(flat)
    if percent:
        try:
            out["discount_percent"] = float(percent.group(1))
        except ValueError:
            pass
    amounts = _classified_amounts(flat, matches)
    # The current price: an explicitly labelled one. Two different
    # labelled currents (e.g. two listings each with "Price:") is
    # ambiguous — return None rather than pick the first.
    currents = [a for a in amounts if a["kind"] == "current"]
    if len(currents) == 1:
        out["current"] = currents[0]["value"]
        out["current_source"] = "labelled"
        out["currency"] = currents[0]["symbol"]
    elif not currents and len(amounts) == 1 and amounts[0]["kind"] == "unlabelled":
        # One amount on the whole page, with no exclusion label: the
        # single-amount fallback (a one-line observation such as
        # "₹100"). Any exclusion label on that lone amount keeps it
        # out — e.g. a lone "MRP ₹9,999" is not a price.
        out["current"] = amounts[0]["value"]
        out["current_source"] = "single-amount"
        out["currency"] = amounts[0]["symbol"]
    originals = [a for a in amounts if a["kind"] == "original"]
    if originals:
        out["original"] = originals[0]["value"]
    discounts = [a for a in amounts if a["kind"] == "discount"]
    if discounts:
        out["discount_amount"] = discounts[0]["value"]
    taxes = [a for a in amounts if a["kind"] == "taxes"]
    if taxes:
        out["taxes"] = taxes[0]["value"]
    for a in amounts:
        if a["kind"] == "other":
            out["other"].append({"label": a["near"][:60], "value": a["value"]})
        elif a["kind"] == "filter-bound":
            out["other"].append({"label": "price filter bound", "value": a["value"]})
    if out["currency"] is None and amounts:
        out["currency"] = amounts[0]["symbol"]
    if out["current"] is None:
        # Why there is no current price — so callers can report the
        # reason instead of guessing. (Stream A: a null current must
        # always carry its reason.)
        if not amounts:
            out["current_reason"] = "no-parseable-amounts"
        elif len(currents) > 1:
            out["current_reason"] = "ambiguous-multiple-labelled-current"
        elif all(a["kind"] in ("original", "discount", "taxes", "other", "filter-bound")
                 for a in amounts):
            out["current_reason"] = "only-excluded-amounts"
        elif len(amounts) > 1:
            out["current_reason"] = "ambiguous-multiple-unlabelled"
        else:
            out["current_reason"] = "unknown"
    return out


# ---------------------------------------------------------------------------
# Structured price record for the BROWSER research path (Stream A,
# issue 4).
#
# The label-aware disambiguator lives in the HTTP monitor path, but
# the browser path only ever carried a bare price string (or nothing)
# into the planner: ``extract_page_research`` returned None whenever
# no current price could be picked, so a page with several variant
# prices — or with only an MRP — produced NO record at all, and the
# planner never learned the product identity, the variant, the
# currency, or the availability alongside the price. The record below
# is always produced, with every field present: product identity +
# variant + currency + availability + current selling price TOGETHER,
# and any field that is genuinely absent is null with a reason —
# never a first-currency-match guess.
#
# The browser observation carries only page text (the extension
# sends no JSON-LD / microdata), so the record is built from the
# label-disambiguated text amounts; when the caller DOES supply
# structured metadata (``structured_price``, as the monitor's
# structured extractor produces), it wins, exactly as in
# ``extract_page_research``. Emitted on every build as a
# ``PRICE_EXTRACT`` log line (CONTRACTS6 vocabulary).
# ---------------------------------------------------------------------------

_logger = logging.getLogger(__name__)

# Generic variant tokens: storage size, RAM size, colour label.
# Deliberately product- and site-agnostic — these are the dimensions
# a store page varies a price by, whatever the product is.
_VARIANT_TOKEN_RES = (
    re.compile(r"\b(32|64|128|256|512|1024)\s?(GB|TB)\b", re.I),
    re.compile(r"\b(4|6|8|12|16|24|32)\s?GB\s+RAM\b", re.I),
    # Colour names are Title Case on store pages ("Midnight Black");
    # capping the capture at two Title-Case words stops it swallowing
    # the words after the name ("Colour: Midnight Black in stock"
    # used to capture "Midnight Black in stock"). No terminator is
    # required after the name — "Colour: Midnight Black Selling
    # Price …" must still match.
    re.compile(r"\b(?i:Color|Colour)\s*:\s*([A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+)?)", 0),
)


def _clean_product_title(title: str, page_text: str) -> str | None:
    """Title minus a trailing " - <site>" suffix (registry-derived,
    never a hard-coded store list); falls back to the first long
    line of page text. None when nothing usable exists."""
    cleaned = (title or "").strip()
    cleaned = re.sub(
        r"\s*[-|–]\s*(?:(?i:" + _RETAILER_DISPLAY_NAMES + r")|[a-z][a-z0-9\-]*(?:\.[a-z]{2,})+).*$",
        "", cleaned,
    ).strip()
    if len(cleaned) < 5:
        first_line = (page_text or "").splitlines()[0] if (page_text or "").splitlines() else ""
        if len(first_line) > 5:
            cleaned = first_line[:100].strip()
    return cleaned if len(cleaned) >= 5 else None


def extract_variant_prices(text: str) -> list[dict[str, Any]]:
    """Pair each price on the page with the variant it belongs to.

    Every variant token (storage / RAM / colour) immediately before
    an amount — within 120 chars and after the previous amount —
    owns that amount: ``128GB ₹1,29,999 · 256GB ₹1,49,999`` yields
    two pairs, so the two variants never collapse into one price,
    and ``(128 GB) (8 GB RAM) ₹22,999`` pairs the one price with
    both dimensions of its configuration. Amounts classified as MRP
    / discount / tax / other by :func:`disambiguate_prices` are
    excluded (they are not a variant's selling price). Returns
    ``[{variant, price, currency}]`` in page order, deduplicated. A
    token AFTER its amount (``₹1,29,999 (128GB)``) is not paired —
    documented limitation.
    """
    if not text:
        return []
    flat = re.sub(r"\s+", " ", text)
    amounts = _classified_amounts(flat, list(_AMOUNT_RE.finditer(flat)))
    tokens: list[tuple[int, int, str]] = []
    for token_re in _VARIANT_TOKEN_RES:
        for m in token_re.finditer(flat):
            label = " ".join(m.group(0).split())
            if token_re is _VARIANT_TOKEN_RES[2]:
                label = " ".join(m.group(1).split())
            if label:
                tokens.append((m.start(), m.end(), label))
    tokens.sort()
    pairs: list[dict[str, Any]] = []
    seen: set[tuple[str, float]] = set()
    for i, a in enumerate(amounts):
        # Exclusion is by label-kind, NOT by value: a variant price
        # that happens to equal the MRP (256GB ₹1,49,999 where the
        # MRP is also ₹1,49,999) is still that variant's price.
        if a["kind"] not in ("current", "unlabelled"):
            continue
        value = a["value"]
        prev_end = amounts[i - 1]["end"] if i else 0
        owners = [t for t in tokens
                  if prev_end <= t[1] <= a["start"]
                  and a["start"] - t[1] <= 120]
        for _, _, variant in owners:
            key = (variant, value)
            if key in seen:
                continue
            seen.add(key)
            pairs.append({"variant": variant, "price": value,
                          "currency": _normalize_currency(a["symbol"])})
    return pairs


def _availability_from_text(text: str) -> str | None:
    """Tri-state availability: "out_of_stock" / "in_stock" / None
    (no signal on the page — null, not assumed in stock)."""
    if re.search(r"(?i)\b(out of stock|currently unavailable|sold out)\b", text or ""):
        return "out_of_stock"
    if re.search(r"(?i)\b(in stock|available)\b", text or ""):
        return "in_stock"
    return None


def build_price_record(
    url: str,
    title: str,
    page_text: str,
    structured_price: Optional[dict[str, Any]] = None,
    *,
    source: str = "browser",
    task_id: str | None = None,
) -> dict[str, Any]:
    """One structured price record for a browser-observed page.

    Always returns the full record — product, variant, currency,
    current selling price, original / discount / tax breakdown,
    availability, price source — with nulls + ``reason`` when a
    field is genuinely absent. Precedence is structured metadata
    first, then the label-disambiguated current price, then (for a
    single unambiguous amount) the single-amount fallback; there is
    NO first-currency-match fallback. ``variant`` is the variant tied
    to the current price; when the current price itself is ambiguous
    (several variants priced differently), ``variant`` is null and
    ``variant_prices`` carries every variant→price pair instead.
    Also logs one ``PRICE_EXTRACT`` line (CONTRACTS6).
    """
    full_text = f"{title or ''}\n{page_text or ''}"
    prices = disambiguate_prices(full_text)
    current: float | None = None
    price_source: str | None = None
    currency: str | None = None
    if isinstance(structured_price, dict):
        try:
            candidate = float(structured_price.get("price"))
        except (TypeError, ValueError):
            candidate = None
        if candidate is not None and 0 < candidate <= _MAX_SANE_PRICE:
            current = candidate
            price_source = str(structured_price.get("source") or "structured")
            currency = _normalize_currency(str(structured_price.get("currency") or ""))
    if current is None and prices.get("current") is not None:
        current = float(prices["current"])
        price_source = ("labelled" if prices.get("current_source") == "labelled"
                        else "single-amount")
    if currency is None:
        currency = _normalize_currency(prices.get("currency"))
    variant_prices = extract_variant_prices(full_text)
    variant: str | None = None
    if current is not None:
        tied = [vp for vp in variant_prices if vp["price"] == current]
        if tied:
            variant = tied[0]["variant"]
    reason = None if current is not None else prices.get("current_reason")
    record: dict[str, Any] = {
        "product": _clean_product_title(title or "", page_text or ""),
        "variant": variant,
        "variant_prices": variant_prices,
        "currency": currency,
        "current": current,
        "original": prices.get("original"),
        "discount_amount": prices.get("discount_amount"),
        "discount_percent": prices.get("discount_percent"),
        "tax": prices.get("taxes"),
        "other": prices.get("other") or [],
        "availability": _availability_from_text(full_text),
        "price_source": price_source,
        "reason": reason,
        "url": url,
    }
    _log = (lambda v: "null" if v is None else str(v).replace(" ", "_"))
    _logger.info(
        "PRICE_EXTRACT source=%s task_id=%s product=%s variant=%s currency=%s "
        "current=%s original=%s discount=%s tax=%s availability=%s",
        source, task_id or "unknown", _log(record["product"]),
        _log(record["variant"]), _log(record["currency"]),
        _log(record["current"]), _log(record["original"]),
        _log(record["discount_amount"]), _log(record["tax"]),
        _log(record["availability"]),
    )
    return record


def extract_page_research(
    url: str,
    title: str,
    page_text: str,
    interactive_elements: Optional[list[dict[str, Any]]] = None,
    image_url: Optional[str] = None,
    structured_price: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Extract structured research entity (price, variant, seller, url) from visited pages.

    Returns None if the page does not appear to be a product, booking, or research destination.

    Price precedence (Stream 2): structured metadata first
    (``structured_price`` — JSON-LD / microdata / meta, as produced by
    the monitor's structured extractor; the agentLoop call site does
    not pass it yet, so the text path below is the live one), then the
    label-disambiguated current price from :func:`disambiguate_prices`
    — never the first currency match in the text. The returned dict
    carries the full breakdown under ``"prices"`` (current / original
    MRP / discount / taxes / other amounts) so a comparison table can
    show what each number is instead of silently quoting an MRP as
    the price.
    """
    if not url or not page_text:
        return None

    parsed_url = urllib.parse.urlparse(url)
    domain = parsed_url.netloc.lower().replace("www.", "")
    if not domain or domain in ("google.com", "bing.com", "duckduckgo.com"):
        return None

    full_text = f"{title}\n{page_text}"

    # Structured price record (Stream A, issue 4) — built BEFORE the
    # price gate below, deliberately: a page with no current price
    # (MRP-only, ambiguous variants) still produces its record and
    # its PRICE_EXTRACT log line, so the loop's extraction attempt is
    # observable even when the fact itself is None. The record's
    # current/price_source agree with the fact's price below (same
    # precedence: structured metadata, then label-disambiguated).
    record = build_price_record(url, title, page_text,
                                structured_price=structured_price)

    # Price: structured metadata wins when the caller supplies it;
    # otherwise the current price is the label-disambiguated one.
    # There is deliberately no first-currency-match fallback: a page
    # whose amounts are all MRP / tax / shipping / other-listing
    # amounts has NO known current price, and returning one of those
    # as "the price" is worse than returning None.
    prices = disambiguate_prices(full_text)
    price_val: float | None = None
    price_source: str | None = None
    if isinstance(structured_price, dict):
        try:
            candidate = float(structured_price.get("price"))
        except (TypeError, ValueError):
            candidate = None
        if candidate is not None and 0 < candidate <= _MAX_SANE_PRICE:
            price_val = candidate
            price_source = str(structured_price.get("source") or "structured")
    if price_val is None and prices.get("current") is not None:
        price_val = float(prices["current"])
        price_source = "labelled" if prices.get("current_source") == "labelled" else "single-amount"
    price_str = None
    if price_val is not None:
        symbol = prices.get("currency") or "₹"
        symbol = "₹" if symbol in ("₹", "Rs", "Rs.", "INR") else ("$" if symbol in ("$", "USD") else symbol)
        if float(price_val).is_integer():
            price_str = f"{symbol}{price_val:,.0f}"
        else:
            price_str = f"{symbol}{price_val:,.2f}"

    if not price_str:
        return None

    # The record was already built (and logged) above, before the
    # price gate — it is attached to the fact here so the planner
    # sees it inside <collected-research>.
    # Clean product title: strip a trailing " - <site>" suffix, where the
    # site is a registry retail name or a domain (Task 8: no hard-coded
    # store list). A capitalised non-site word ("Silver") is kept.
    cleaned_title = _clean_product_title(title, page_text) or title.strip()

    # Variant extraction: RAM, Storage, Color, Generation
    variants = []
    storage_match = re.search(r"\b(32|64|128|256|512)\s?(?:GB|TB)\b", full_text, re.IGNORECASE)
    if storage_match:
        variants.append(storage_match.group(0).upper())

    ram_match = re.search(r"\b(4|6|8|12|16)\s?(?:GB)\s+RAM\b", full_text, re.IGNORECASE)
    if ram_match:
        variants.append(ram_match.group(0).upper())

    # Colour: Title-Case name, at most two words — the old
    # ``[A-Za-z\s]+?`` capture swallowed trailing prose on flat text
    # ("Colour: Midnight Black in stock" → "Midnight Black in stock").
    color_match = re.search(r"\b(?i:Color|Colour):\s*([A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+)?)", full_text)
    if color_match:
        color = color_match.group(1).strip()
        if len(color) < 25:
            variants.append(color)

    # Seller extraction
    seller = None
    seller_match = re.search(r"(?i)(?:Sold by|Seller:)\s*([A-Za-z0-9\s,\.\-]{3,40}?)(?:\s*(?:and Fulfilled|Free Delivery|\.|\n|$))", full_text)
    if seller_match:
        seller = seller_match.group(1).strip()
    else:
        # Task 8: the seller is the site's display name from the
        # registry, not a hard-coded branch per retailer.
        for _entry in KNOWN_DOMAINS_REGISTRY.values():
            if domain == _entry["primary_domain"] or domain in _entry.get("allowed_domains", []):
                seller = f"{_entry.get('display_name') or _entry['name']} Seller"
                break

    # Availability
    in_stock = True
    if re.search(r"(?i)\b(out of stock|currently unavailable|sold out)\b", full_text):
        in_stock = False

    return {
        "domain": domain,
        "url": url,
        "title": cleaned_title,
        "price": price_str,
        # Where the price came from, and what every other amount on
        # the page is (MRP / discount / taxes / shipping / other
        # listings) — so a comparison never quotes an MRP as a price.
        "price_source": price_source,
        "prices": prices,
        # Full structured record (issue 4): identity + variant +
        # currency + availability + current price in one place, with
        # nulls + reason when a field is absent. The variant tied to
        # the current price wins over the first variant mentioned in
        # the text; variant_prices keeps every variant→price pair.
        "price_record": record,
        "variant_prices": record["variant_prices"],
        "variant": record["variant"] or (", ".join(variants) if variants else "Standard"),
        "seller": seller or domain.capitalize(),
        "in_stock": in_stock,
        # Product image from the page's og:image, validated above.
        # Absent (None) when the page declares none or it is unsafe.
        "image": _valid_image_url(image_url),
    }
