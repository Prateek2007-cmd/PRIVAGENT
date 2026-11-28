"""HTTP-based monitor check for public pages (Task 2 live monitoring).

A watch on a plain public ``http(s)`` URL does not need a live Chrome
extension: this module fetches the page over HTTP, extracts its visible
text, accessible element names and price, and feeds the observation to
the existing :func:`app.change_detector.evaluate_observation`.

Safety properties (read-only by construction):

* **GET only.** No writes, no payments, no navigation that submits
  anything, no cookies or credentials are ever sent.
* **SSRF-guarded.** Only ``http``/``https``; no userinfo in the URL;
  IP-literal hosts must be public, except ``127.0.0.1`` which is the
  fixture address used by the tests and the demo script. Non-canonical
  IPv4 forms (decimal ``2130706433``, hex ``0x7f000001``, octal
  ``0177.0.0.1``, short ``127.1``) are normalized to the canonical
  dotted quad before the check, and a trailing dot is stripped, so
  neither spelling can smuggle a private address past the gate.
  Every redirect hop is validated the same way (max 5). For hostname
  targets the DNS rebinding check runs only when the fetch would go
  direct; when an HTTP(S) proxy is configured the proxy resolves the
  host, and this VM's local DNS answers every public name with a
  CGNAT address (``198.18.0.0/15``), so a local DNS check would
  reject every real page. (In practice the check never runs on Linux
  — see :func:`_validate_monitor_url` — so the literal/canonical-IP
  checks above are the operative defence and the rebinding defence
  for proxied fetches rests on the egress proxy.)
* **Bounded streaming.** ~10 s timeout; the body is streamed in
  64 KiB chunks and parsed incrementally, so a 1.75 MB page (ISRO
  opportunities) is checked, not refused. The raw stream is capped
  at 8 MiB (truncated, never refused for size alone) and the
  extracted text at 200 KiB; only main content is kept (see below).
* **Main-content extraction.** ``<main>``/``<article>`` content is
  preferred when present; nav/footer/header/aside/script/style and
  ad/cookie/popup/newsletter boilerplate (by tag and by class/id)
  are skipped, so ads and chrome never enter the baseline hash.
* **Product-specific price.** The price comes from structured
  metadata first — JSON-LD Product/Offer, microdata
  ``itemprop=price``, ``og:price:amount`` / ``product:price:amount``
  meta, price-classed elements, or a JSON product API
  (``lowest_price`` / seller prices) — with the product name
  recorded for the identity check. A bare first-currency-match is
  never used when several amounts compete (see
  :func:`app.change_detector._extract_price`).
* **Redacted before storing.** The observation goes through the same
  :func:`app.change_detector._state` redaction as the browser path;
  nothing here persists page text beyond the redacted baseline and
  the 250-char diff excerpt that detector already stores.

Uses only the standard library — no new dependency.
"""
from __future__ import annotations

import codecs
import html
import ipaddress
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any

FETCH_TIMEOUT_S = 10.0
# Stream A: the old 1 MiB hard refusal killed every check of the
# 1,751,390-byte ISRO opportunities page. The body is now STREAMED
# and parsed incrementally (see _fetch_once): this is the most raw
# bytes ever read, and oversize input is truncated, not refused.
# 8 MiB bounds memory on a hostile page while covering every real
# listing page seen so far by 4x.
MAX_STREAM_BYTES = 8_388_608
MAX_BODY_BYTES = MAX_STREAM_BYTES  # backwards-compatible alias
MAX_REDIRECTS = 5
_READ_CHUNK = 65_536
# Extracted visible text is capped so a huge page cannot bloat the
# observation (the detector redacts the state to 4,000 chars anyway).
MAX_EXTRACTED_CHARS = 200_000

# 127.0.0.1 is the fixture address used by the tests and the demo
# script; every other loopback/private/link-local/reserved address
# is refused.
_FIXTURE_HOST = "127.0.0.1"

_INTERNAL_HOSTNAMES = {
    "localhost",
    "::1",
    "0.0.0.0",
    "metadata.google.internal",
    "instance-data",
    "metadata",
}

# Same embedded-private-IP guard as website_resolver.is_safe_public_url.
_EMBEDDED_PRIVATE_IP_RE = re.compile(
    r"(?:127(?:\.\d+){3}|10(?:\.\d+){3}|192\.168(?:\.\d+){2}"
    r"|172\.(?:1[6-9]|2\d|3[01])(?:\.\d+){2}|169\.254(?:\.\d+){2})"
)


def _canonical_ipv4(hostname: str) -> str | None:
    """Canonical dotted-quad for an ``inet_aton``-style numeric host.

    :func:`ipaddress.ip_address` only accepts canonical dotted quads,
    but resolvers (and therefore urllib) also accept decimal
    (``2130706433``), hex (``0x7f000001``), octal (``0177.0.0.1``) and
    short (``127.1``) forms — all of which sailed past the IP-literal
    check below and were only caught by the DNS-rebinding check, which
    in practice never runs (see :func:`_validate_monitor_url`).
    Normalizing here makes the IP check see the address the resolver
    will actually use. Returns None for ordinary hostnames.
    """
    try:
        packed = socket.inet_aton(hostname)
    except (OSError, ValueError):
        return None
    return socket.inet_ntoa(packed)


def _ip_is_public_or_fixture(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> tuple[bool, str | None]:
    """True for a public address, or the 127.0.0.1 fixture address."""
    if str(ip) == _FIXTURE_HOST:
        return True, None
    if isinstance(ip, ipaddress.IPv6Address):
        # NAT64 / IPv4-mapped IPv6: judge the embedded IPv4 address.
        for prefix in (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("::ffff:0:0/96")):
            if ip in prefix:
                embedded = ipaddress.ip_address(ip.packed[-4:])
                return _ip_is_public_or_fixture(embedded)
    if (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved
            or ip.is_multicast or str(ip) == "169.254.169.254"):
        return False, f"Address '{ip}' is private, loopback, or cloud metadata."
    return True, None


def _validate_monitor_url(url: str) -> tuple[bool, str | None]:
    """Fail-closed URL gate for the HTTP monitor check."""
    if not url or not isinstance(url, str):
        return False, "Empty monitor URL."
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except ValueError as exc:
        return False, f"Malformed URL: {exc}"
    if parts.scheme not in ("http", "https"):
        return False, f"Disallowed scheme '{parts.scheme}': only http and https are permitted."
    if parts.username or parts.password:
        return False, "Embedded credentials in URL authority are prohibited."
    # A trailing dot is insignificant in DNS ("localhost." is
    # "localhost") but defeated the internal-hostname check below, so
    # strip it before any hostname comparison.
    hostname = (parts.hostname or "").lower().strip().rstrip(".")
    if not hostname:
        return False, "Empty hostname."
    if (hostname in _INTERNAL_HOSTNAMES or hostname.endswith(".internal")
            or hostname.endswith(".local") or hostname.endswith(".localhost")):
        return False, f"Access to private/internal host '{hostname}' is blocked."
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        ip = None
    if ip is None:
        # Non-canonical IPv4 forms (decimal / hex / octal / short):
        # judge the canonical address the resolver will use.
        canonical = _canonical_ipv4(hostname)
        if canonical is not None:
            ip = ipaddress.ip_address(canonical)
    if ip is not None:
        return _ip_is_public_or_fixture(ip)
    if _EMBEDDED_PRIVATE_IP_RE.search(hostname):
        return False, f"Embedded private IP pattern in hostname '{hostname}' is blocked."
    if "%" in hostname:
        # A '%' here is never a legitimate DNS name (a valid IPv6
        # zone id was already consumed by the IP-literal check above):
        # it is a percent-encoded host such as %31%32%37.0.0.1, which
        # no resolver will answer for. Reject rather than pass an
        # unparseable host to the fetcher.
        return False, f"Percent-encoded hostname '{hostname}' is blocked."
    # DNS rebinding defence, only when the fetch goes direct. When a
    # proxy is configured for this URL the proxy resolves the host;
    # local DNS in this VM answers every public name with a CGNAT
    # address, so checking it here would reject every real page.
    # NOTE (2026-11-27, verified by probe): on Linux
    # ``urllib.request.proxy_bypass`` returns False for every host
    # except those in ``no_proxy`` — and False for ALL hosts when no
    # proxy is configured at all (it falls through to the macOS
    # sysconf path). So in practice this check runs only for
    # ``no_proxy`` hosts (localhost / 127.0.0.1 / ::1 here), which the
    # checks above already handle. The rebinding defence for proxied
    # fetches therefore rests on the egress proxy, and the literal /
    # canonical-IP checks above are the operative defence in this
    # module. Do not "fix" this by always resolving: the local DNS
    # answer (CGNAT 198.18.0.0/15, ``is_private``) would reject every
    # real page.
    if urllib.request.proxy_bypass(hostname):
        try:
            addr_info = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
        except (socket.gaierror, socket.herror, TimeoutError, OSError) as exc:
            return False, f"Hostname '{hostname}' does not resolve ({exc})."
        for *_, sockaddr in addr_info:
            resolved = ipaddress.ip_address(sockaddr[0])
            safe, err = _ip_is_public_or_fixture(resolved)
            if not safe:
                return False, f"Hostname '{hostname}' resolves to private/internal IP '{resolved}' ({err})."
    return True, None


class _ValidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to URLs that pass the monitor URL gate."""

    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        ok, reason = _validate_monitor_url(newurl)
        if not ok:
            raise urllib.error.HTTPError(
                newurl, code, f"Redirect rejected: {reason}", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# Tags whose content is never visible page text.
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "nav", "footer", "header", "aside"}
# Tags whose accessible name is worth keeping as an element name.
_NAME_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "a", "button"}
# Tags that mark the page's main content. When any text is collected
# inside one of these, boilerplate outside it (cookie banners, side
# rails, "related" boxes) is dropped from the observation.
_MAIN_TAGS = {"main", "article"}
# Void elements never get an end tag: they must never be pushed on
# the boilerplate-skip stack, or the depth count desyncs.
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
              "link", "meta", "param", "source", "track", "wbr"}
# class/id fragments that mark ad/consent/promo chrome. Applied to
# container tags only (div/section/aside/ul/span); a product whose
# own name contains one of these words keeps its text because the
# match is on the container's class/id, not on the text.
_BOILERPLATE_RE = re.compile(
    r"(?:^|[-_ ])(?:ads?|advert(?:isement)?|cookie|consent|popup|modal|newsletter|"
    r"banner|sidebar|promo(?:tion)?[-_ ]?banner|share[-_ ]?(?:buttons?|links?)|"
    r"social[-_ ]?(?:share|links|buttons)|paywall|subscribe[-_ ]?(?:box|banner|modal)|"
    r"related[-_ ]?(?:posts|articles|products)|comments?[-_ ]?(?:section|box))"
    r"(?:$|[-_ ])", re.I)
_BOILERPLATE_TAGS = {"div", "section", "aside", "header", "footer", "nav", "ul", "span", "form"}
# class fragments that mark a price element (store product pages,
# tracker pages). The text inside is a price candidate, never the
# page's first currency match.
_PRICE_CLASS_RE = re.compile(r"(?:^|[-_ ])(?:price|selling[-_ ]?price|current[-_ ]?price|"
                             r"offer[-_ ]?price|sale[-_ ]?price|product[-_ ]?price)"
                             r"(?:$|[-_ ])", re.I)
_CURRENCY_SYMBOLS = {"₹": "INR", "$": "USD", "£": "GBP", "€": "EUR"}
_KNOWN_CURRENCIES = {"INR", "USD", "EUR", "GBP", "JPY", "AUD", "CAD"}
_AMOUNT_RE = re.compile(r"([\d,]+(?:\.\d{1,2})?)")
# A price is only valid when it is a plausible product price.
_MAX_SANE_PRICE = 10_000_000.0


def _parse_amount(raw: str | None) -> float | None:
    """'4,199' / '4199.00' / '1,29,999' (Indian grouping) /
    '1.299,99' (European decimal comma) -> float; None when not a
    sane price. Delegates to the grouping-aware parser in
    extractors.research so the monitor path and the browser text
    path parse the same token the same way; plain numeric values
    (JSON-LD ``price`` as a number) pass through directly."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        return value if 0 < value <= _MAX_SANE_PRICE else None
    text = str(raw).strip().replace("\u00a0", "")
    if not text:
        return None
    from .extractors.research import _parse_grouped_amount

    return _parse_grouped_amount(text)


def _amount_from_text(text: str) -> tuple[float | None, str | None]:
    """First currency amount in *text* -> (value, currency-or-None).

    The numeric token is captured whole (Indian grouping
    ``1,29,999``, European ``1.299,99``) and parsed grouping-aware;
    the old ``[\\d,]+(?:\\.\\d{1,2})?`` token truncated
    ``€1.299,99`` to 1.29."""
    m = re.search(r"(₹|\$|£|€|(?:Rs\.?|INR|USD|EUR|GBP)\s*)\s*([0-9](?:[0-9.,]*[0-9])?)", text or "", re.I)
    if not m:
        return None, None
    from .extractors.research import _parse_grouped_amount

    value = _parse_grouped_amount(m.group(2), m.group(1).strip())
    if value is None:
        return None, None
    symbol = m.group(1).strip()
    currency = _CURRENCY_SYMBOLS.get(symbol) or (symbol.upper() if symbol.upper() in _KNOWN_CURRENCIES else None)
    return value, currency


def _walk_json_ld(node: Any, out: list[dict[str, Any]]) -> None:
    """Collect every dict in a JSON-LD document (walks @graph/lists)."""
    if isinstance(node, dict):
        out.append(node)
        for value in node.values():
            _walk_json_ld(value, out)
    elif isinstance(node, list):
        for item in node:
            _walk_json_ld(item, out)


def _offer_price(offers: Any) -> tuple[float | None, str | None]:
    """Best price from a JSON-LD offers node (Offer / AggregateOffer)."""
    candidates: list[tuple[float, str | None]] = []
    nodes = offers if isinstance(offers, list) else [offers]
    for offer in nodes:
        if not isinstance(offer, dict):
            continue
        if offer.get("@type") == "AggregateOffer":
            value = _parse_amount(offer.get("lowPrice"))
            if value is not None:
                candidates.append((value, str(offer.get("priceCurrency") or "") or None))
            continue
        value = _parse_amount(offer.get("price"))
        if value is not None:
            candidates.append((value, str(offer.get("priceCurrency") or "") or None))
    if not candidates:
        return None, None
    candidates.sort(key=lambda c: c[0])
    return candidates[0]


class _VisibleTextParser(HTMLParser):
    """Extract visible text, element names and structured product
    data from HTML — incrementally, so a multi-MB page is parsed as
    it streams instead of being held whole and refused.

    * ``<main>``/``<article>`` text wins when present; everything
      else is kept only when the page has no main content.
    * Script/style/nav/footer/header/aside and ad/cookie/popup/
      newsletter boilerplate (tag or class/id) are skipped.
    * JSON-LD ``<script type="application/ld+json">`` blocks, meta
      price tags, ``itemprop=price`` microdata and price-classed
      elements are captured for
      :func:`extract_product_price` — the price never comes from
      the first currency-looking number in the body text.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_stack: list[str] = []
        self._main_depth = 0
        self._text_parts: list[str] = []
        self._main_parts: list[str] = []
        self._text_len = 0
        self._elements: list[str] = []
        self._name_tag: str | None = None
        self._name_parts: list[str] = []
        self._title_parts: list[str] = []
        self._h1_parts: list[str] = []
        self._in_h1 = False
        self.ld_json: list[Any] = []
        self._ld_capture = False
        self._ld_parts: list[str] = []
        self.meta: dict[str, str] = {}
        self.microdata_price: tuple[float | None, str | None] = (None, None)
        self._micro_capture = False
        self._micro_tag: str | None = None
        self._micro_parts: list[str] = []
        self._micro_currency: str | None = None
        self.class_prices: list[tuple[float, str | None]] = []
        self._price_stack: list[tuple[str, list[str]]] = []

    # -- helpers ----------------------------------------------------
    def _skipped(self) -> bool:
        return bool(self._skip_stack)

    def _append_text(self, text: str) -> None:
        if self._text_len >= MAX_EXTRACTED_CHARS:
            return
        text = text[: MAX_EXTRACTED_CHARS - self._text_len]
        self._text_len += len(text)
        if self._main_depth:
            self._main_parts.append(text)
        else:
            self._text_parts.append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        # JSON-LD is data, not boilerplate: capture it even though
        # <script> is otherwise skipped.
        if tag == "script" and (attrs_dict.get("type") or "").lower() == "application/ld+json":
            self._ld_capture = True
            self._ld_parts = []
            return
        if tag in _MAIN_TAGS and not self._skipped():
            self._main_depth += 1
        if tag in _SKIP_TAGS:
            if tag not in _VOID_TAGS:
                self._skip_stack.append(tag)
            return
        if tag in _BOILERPLATE_TAGS:
            haystack = f"{attrs_dict.get('class') or ''} {attrs_dict.get('id') or ''}"
            if _BOILERPLATE_RE.search(haystack):
                self._skip_stack.append(tag)
                return
        if self._skipped():
            return
        if tag == "meta":
            key = (attrs_dict.get("property") or attrs_dict.get("name")
                   or attrs_dict.get("itemprop") or "").lower()
            if key and attrs_dict.get("content"):
                self.meta.setdefault(key, str(attrs_dict["content"]))
            if (attrs_dict.get("itemprop") or "").lower() == "price":
                self.microdata_price = (_parse_amount(attrs_dict.get("content")),
                                        self._micro_currency)
            if (attrs_dict.get("itemprop") or "").lower() == "pricecurrency":
                self._micro_currency = (attrs_dict.get("content") or "").strip().upper() or None
            return
        if (attrs_dict.get("itemprop") or "").lower() == "price":
            content = attrs_dict.get("content")
            value = _parse_amount(content) if content else None
            if value is not None:
                self.microdata_price = (value, self._micro_currency)
            else:
                self._micro_capture = True
                self._micro_tag = tag
                self._micro_parts = []
        price_class = _PRICE_CLASS_RE.search(attrs_dict.get("class") or "")
        if price_class and tag not in _VOID_TAGS:
            self._price_stack.append((tag, []))
        label = attrs_dict.get("aria-label") or attrs_dict.get("title") or attrs_dict.get("alt")
        if label and tag in ("a", "button", "img", "input", "select", "textarea"):
            self._elements.append(str(label).strip())
        if tag in _NAME_TAGS:
            self._name_tag = tag
            self._name_parts = []
        if tag == "h1":
            self._in_h1 = True
            self._h1_parts = []
        if tag == "title":
            self._name_tag = "title"
            self._title_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._ld_capture:
            self._ld_capture = False
            raw = "".join(self._ld_parts).strip()
            if raw:
                try:
                    self.ld_json.append(json.loads(raw))
                except (ValueError, TypeError):
                    pass
            return
        if tag in _MAIN_TAGS and self._main_depth and not self._skipped():
            self._main_depth -= 1
        if self._skip_stack and tag == self._skip_stack[-1]:
            self._skip_stack.pop()
            return
        if self._skipped():
            return
        if tag == "h1" and self._in_h1:
            self._in_h1 = False
        if self._micro_capture and tag == self._micro_tag:
            value, _ = _amount_from_text(" ".join(self._micro_parts))
            if value is not None:
                self.microdata_price = (value, self._micro_currency)
            self._micro_capture = False
            self._micro_tag = None
        if self._price_stack and tag == self._price_stack[-1][0]:
            _, parts = self._price_stack.pop()
            value, currency = _amount_from_text(" ".join(parts))
            if value is not None:
                self.class_prices.append((value, currency))
        if tag == "title" and self._name_tag == "title":
            # Title is prepended in result(); it is not a body node.
            self._name_tag = None
        elif tag in _NAME_TAGS and self._name_tag == tag:
            name = " ".join(" ".join(self._name_parts).split())
            if name:
                self._elements.append(name)
            self._name_tag = None

    def handle_data(self, data: str) -> None:
        if self._ld_capture:
            self._ld_parts.append(data)
            return
        if self._skipped():
            return
        text = data.strip()
        if not text:
            return
        if self._micro_capture:
            self._micro_parts.append(text)
        for _, parts in self._price_stack:
            parts.append(text)
        if self._name_tag == "title":
            # Title text is inserted once at the front on </title>;
            # do not also keep it as a body text node.
            self._title_parts.append(text)
            return
        if self._in_h1:
            self._h1_parts.append(text)
        self._append_text(text)
        if self._name_tag in _NAME_TAGS:
            self._name_parts.append(text)

    def result(self) -> dict[str, Any]:
        # Main content wins when the page has any; otherwise the
        # whole visible text. The <title> leads either way.
        parts = self._main_parts if self._main_parts else self._text_parts
        title = " ".join(" ".join(self._title_parts).split())
        text = " ".join(" ".join(([title] if title else []) + parts).split())
        elements = [e for e in (e.strip() for e in self._elements) if e][:100]
        h1 = " ".join(" ".join(self._h1_parts).split())
        out: dict[str, Any] = {"text": text, "elements": elements,
                               "used_main_content": bool(self._main_parts)}
        if title:
            out["page_title"] = title
        if h1:
            out["h1"] = h1
        price_info = extract_product_price(self)
        if price_info:
            out["price_info"] = price_info
            if price_info.get("price") is not None:
                out["price"] = price_info["price"]
            if price_info.get("name"):
                out["product_name"] = price_info["name"]
        return out


def extract_product_price(parser: _VisibleTextParser) -> dict[str, Any] | None:
    """Product-specific price from structured metadata — never the
    first currency match in body text.

    Precedence: JSON-LD Product/Offer (or AggregateOffer.lowPrice) >
    microdata itemprop=price > og:price:amount /
    product:price:amount meta > price-classed elements. The product
    name comes from the same JSON-LD Product node, else og:title /
    <title> / first <h1>, so the caller can check the price belongs
    to the watched product. Returns None when no validated price
    field exists (the detector then reports 'Price unavailable'
    instead of guessing).
    """
    name: str | None = None
    for doc in parser.ld_json:
        nodes: list[dict[str, Any]] = []
        _walk_json_ld(doc, nodes)
        for node in nodes:
            types = node.get("@type")
            type_list = types if isinstance(types, list) else [types]
            if "Product" not in type_list:
                continue
            if not name and isinstance(node.get("name"), str):
                name = str(node["name"]).strip()
            offers = node.get("offers")
            if offers is not None:
                value, currency = _offer_price(offers)
                if value is not None:
                    return {"price": value, "currency": currency,
                            "name": name, "source": "json-ld"}
    value, currency = parser.microdata_price
    if value is not None:
        return {"price": value, "currency": currency,
                "name": name or parser.meta.get("og:title"),
                "source": "microdata"}
    for key in ("og:price:amount", "product:price:amount"):
        value = _parse_amount(parser.meta.get(key))
        if value is not None:
            currency = (parser.meta.get("og:price:currency")
                        or parser.meta.get("product:price:currency") or None)
            return {"price": value, "currency": currency,
                    "name": name or parser.meta.get("og:title"),
                    "source": "meta"}
    if parser.class_prices:
        # Cheapest price-classed amount: a page that lists an MRP
        # next to the selling price reports the selling price.
        value, currency = sorted(parser.class_prices, key=lambda c: c[0])[0]
        return {"price": value, "currency": currency,
                "name": name or parser.meta.get("og:title"),
                "source": "price-element"}
    if not name:
        name = parser.meta.get("og:title") or None
    title = " ".join(" ".join(parser._title_parts).split())
    h1 = " ".join(" ".join(parser._h1_parts).split())
    return {"price": None, "currency": None,
            "name": name or h1 or title or None, "source": None} if (name or h1 or title) else None


def extract_visible(html_text: str) -> dict[str, Any]:
    """Visible text + element names from an HTML document (stdlib only)."""
    parser = _VisibleTextParser()
    parser.feed(html_text)
    parser.close()
    return parser.result()


def extract_json_product(payload: Any) -> dict[str, Any]:
    """Observation for a JSON product API (e.g. ShopSphere
    ``GET /api/v1/products/{id}``): the price is the validated
    ``lowest_price`` / cheapest seller price, the identity is the
    ``name`` field — never a currency scan of the raw JSON.

    Accepts a bare product dict or a ``{"product": {...}}`` envelope.
    Returns {} when the payload carries no product.
    """
    product = payload.get("product") if isinstance(payload, dict) else None
    if not isinstance(product, dict):
        product = payload if isinstance(payload, dict) else None
    if not isinstance(product, dict):
        return {}
    name = product.get("name") or product.get("title")
    if not name:
        # No product identity, no price: a bare {"price": 1299}
        # (or any anonymous price field) is not a product observation
        # — accepting it would be the JSON form of first-match.
        return {}
    sellers = product.get("sellers") or []
    seller_prices: list[float] = []
    seller_lines: list[str] = []
    for seller in sellers if isinstance(sellers, list) else []:
        if not isinstance(seller, dict):
            continue
        value = _parse_amount(seller.get("price"))
        if value is None:
            continue
        seller_prices.append(value)
        seller_lines.append(
            f"{seller.get('name') or 'Seller'} ₹{value:g}"
            f"{' · out of stock' if seller.get('stock') == 0 else ''}")
    price: float | None = _parse_amount(product.get("lowest_price"))
    if price is None and seller_prices:
        price = min(seller_prices)
    if price is None:
        price = _parse_amount(product.get("price"))
    if price is not None and seller_prices and price not in seller_prices:
        # lowest_price must be a real seller price, or it is stale
        # metadata — fall back to the cheapest seller actually listed.
        price = min(seller_prices)
    text_bits = [str(name or ""), str(product.get("description") or "")]
    text_bits.extend(seller_lines)
    text = " ".join(" ".join(text_bits).split())
    out: dict[str, Any] = {"text": text,
                           "elements": [str(name or "")] + [s.get("name", "") for s in sellers
                                                             if isinstance(s, dict) and s.get("name")][:20],
                           "used_main_content": False}
    if name:
        out["product_name"] = str(name)
    if price is not None:
        out["price"] = price
        out["price_info"] = {"price": price, "currency": "INR",
                             "name": str(name) if name else None,
                             "source": "json-api"}
    return out


# A transient transport blip (DNS lookup failed, connection reset)
# must not kill a watch: one failed fetch retries twice with a
# growing backoff before the scheduler ever hears about it.
_FETCH_ATTEMPTS = 3
_FETCH_BACKOFF_S = (2.0, 5.0)


def _fetch_once(url: str, *, timeout: float, max_bytes: int) -> dict[str, Any]:
    """Single GET attempt. Raises ValueError on a refused URL or a
    non-HTML/non-JSON body, RuntimeError on an HTTP failure, and
    lets URLError/timeout propagate so the caller can retry a
    transient transport blip.

    Stream A: the body is streamed in 64 KiB chunks and fed to the
    parser as it arrives — a 1.75 MB page is checked, not refused.
    Reading stops at *max_bytes* (truncated, with
    ``truncated: True`` on the observation); the parser also caps
    retained text, so memory stays bounded on any page.
    """
    ok, reason = _validate_monitor_url(url)
    if not ok:
        raise ValueError(f"Monitor HTTP check refused {url}: {reason}")
    request = urllib.request.Request(url, method="GET", headers={
        "User-Agent": "PrivAgent-monitor/1.0 (read-only watch; +https://github.com/Prateek2007-cmd/PRIVAGENT)",
        "Accept": "text/html, application/json;q=0.9, text/plain;q=0.9",
    })
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler(), _ValidatingRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            content_type = (response.headers.get_content_type() or "").lower()
            if content_type in ("application/json", "text/json"):
                chunks: list[bytes] = []
                total = 0
                truncated = False
                while True:
                    chunk = response.read(_READ_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        truncated = True
                        break
                    chunks.append(chunk)
                try:
                    payload = json.loads(b"".join(chunks).decode("utf-8", errors="replace"))
                except ValueError as exc:
                    raise ValueError(f"Monitor HTTP check refused unparseable JSON from {url}") from exc
                observation = extract_json_product(payload)
                if not observation:
                    raise ValueError(f"Monitor HTTP check refused JSON with no product from {url}")
                observation["url"] = url
                observation["truncated"] = truncated
                return observation
            if content_type not in ("text/html", "text/plain"):
                raise ValueError(f"Monitor HTTP check refused non-HTML content type '{content_type}'")
            charset = response.headers.get_content_charset() or "utf-8"
            decoder = codecs.getincrementaldecoder(charset)(errors="replace")
            parser = _VisibleTextParser()
            total = 0
            truncated = False
            while True:
                chunk = response.read(_READ_CHUNK)
                if not chunk:
                    break
                remaining = max_bytes - total
                if remaining <= 0:
                    truncated = True
                    break
                total += len(chunk)
                if len(chunk) > remaining:
                    # The chunk that crosses the cap is still parsed
                    # up to the cap — truncating must not discard
                    # everything read so far.
                    parser.feed(decoder.decode(chunk[:remaining]))
                    truncated = True
                    break
                parser.feed(decoder.decode(chunk))
            parser.feed(decoder.decode(b"", final=True))
            parser.close()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Monitor HTTP check failed for {url}: HTTP {exc.code}") from exc
    observation = parser.result()
    observation["text"] = html.unescape(observation["text"])
    observation["url"] = url
    observation["truncated"] = truncated
    return observation


def fetch_observation(url: str, *, timeout: float = FETCH_TIMEOUT_S,
                      max_bytes: int = MAX_STREAM_BYTES) -> dict[str, Any]:
    """GET a public page and return the observation for the detector.

    Raises ValueError on a refused URL or a non-HTML/non-JSON body,
    and RuntimeError on transport/HTTP failures. The caller decides
    what a failure means for the mission; nothing is persisted here.

    Transport failures (DNS, connect, timeout) are retried twice
    with a 2s then 5s backoff first: a transient blip must not mark
    a watch failed. Refusals (bad URL, non-HTML) and HTTP errors are
    definitive and raise at once. Oversize bodies are truncated,
    not refused (Stream A).
    """
    for attempt in range(_FETCH_ATTEMPTS):
        try:
            observation = _fetch_once(url, timeout=timeout, max_bytes=max_bytes)
            # R5-02 parity with the browser path (privacy.py): a bare
            # 6-digit run in page text is OTP/PIN-shaped and is masked
            # before the observation reaches the detector — the stored
            # baseline must not keep a one-time code the page happened
            # to display. Structured prices survive (they ride in the
            # price fields, not the text), and the URL itself is never
            # touched here. Applied here, not in _fetch_once, so
            # retries still fetch the raw page.
            text = observation.get("text")
            if isinstance(text, str) and text:
                from .privacy import _redact_bare_otp_codes

                observation["text"] = _redact_bare_otp_codes(text)
            return observation
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            if attempt + 1 >= _FETCH_ATTEMPTS:
                reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
                raise RuntimeError(f"Monitor HTTP check failed for {url}: {reason}") from exc
            time.sleep(_FETCH_BACKOFF_S[attempt])


def check_monitor_http(mission_id: str, url: str, *, db_path: str | None = None,
                       task_id: str | None = None,
                       timeout: float = FETCH_TIMEOUT_S) -> dict[str, Any] | None:
    """One read-only HTTP check cycle for a mission.

    Fetches the page, evaluates it against the stored baseline and
    returns the alert event (``None`` when nothing alert-worthy
    changed). Alerts are audit-logged with metadata only — never page
    text. Raises on fetch/validation failure; the scheduler marks the
    mission failed in that case.
    """
    from .change_detector import evaluate_observation

    observation = fetch_observation(url, timeout=timeout)
    event = evaluate_observation(mission_id, observation, task_id=task_id, db_path=db_path)
    if event:
        from .audit import append_event

        append_event(
            "monitor_alert",
            f"Monitor {mission_id} alert on {url}: {event.get('summary', '')}",
            actor="monitor",
            task_id=task_id,
        )
    return event
