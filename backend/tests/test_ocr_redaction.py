"""Live OCR redaction verification (Stream E, 2026-11-27).

Unlike ``test_stream_e_regressions.py::test_ocr_text_with_otp_is_redacted``,
which *mocks* ``extract_ocr_text`` to return a canned OTP string, this
module renders real text into a PNG and runs the real ``tesseract``
binary over it, then pushes the screenshot through
``classify_and_redact`` + ``build_prompt`` and asserts no secret
survives into the planner prompt.

Secrets rendered on the "page": a labelled OTP (``Your OTP is
482910``), a space-separated labelled password (``Password
hunter2-9x7q``) and a postal address (``221B Baker Street``). The
labelled password is the interesting one: on first run (2026-11-27)
it reached the planner prompt UNREDACTED — ``sensitive_kv`` needs
":" / "=" / "is" between label and value, so a space-separated token
slipped through. That live failure drove the ``labelled_secret``
pattern in privacy.py (label + 8+ char token containing a digit).
The bare password with NO label (``hunter2-9x7q`` alone) is still the
documented 1/24 leak in *page text* — no pattern can match it there
without guessing — but a labelled one in OCR text is now caught,
which is exactly what this test proves: OCR text goes through
``_redact_text`` + bare-OTP masking + ``cleanse_untrusted`` before
it is stored on the payload (privacy.py, R5-05).

Skipped when no ``tesseract`` binary is on PATH: OCR is opt-in and
its absence means "no OCR block", never a failure.
"""
from __future__ import annotations

import base64 as _b64
import io

import pytest

from app import config as app_config
from app.contracts import BrowserObservation
from app.ocr import extract_ocr_text, ocr_available
from app.planner import build_prompt
from app.privacy import classify_and_redact

OTP = "482910"
PASSWORD = "hunter2-9x7q"
ADDRESS = "221B Baker Street"


def _render_secret_png() -> str:
    """White PNG with the three secrets as large black text; returns a
    ``data:image/png;base64,`` URL, as the extension would send it."""
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 40
    )
    img = Image.new("RGB", (900, 220), "white")
    draw = ImageDraw.Draw(img)
    draw.text((20, 20), f"Your OTP is {OTP}", fill="black", font=font)
    draw.text((20, 90), f"Password {PASSWORD}", fill="black", font=font)
    draw.text((20, 160), ADDRESS, fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + _b64.b64encode(buf.getvalue()).decode()


@pytest.mark.skipif(not ocr_available(), reason="tesseract binary not on PATH")
def test_real_tesseract_reads_the_secrets():
    """Sanity: the rendered PNG really OCRs to the secrets. If
    tesseract misreads here, the redaction test below is vacuous, so
    this test must pass first."""
    shot = _render_secret_png()
    text = extract_ocr_text(shot, enabled=True, max_chars=2000)
    assert text, "tesseract returned no text for a clean rendered image"
    assert OTP in text, f"OCR misread the OTP: {text!r}"
    assert PASSWORD in text, f"OCR misread the password: {text!r}"
    assert "Baker Street" in text, f"OCR misread the address: {text!r}"


@pytest.mark.skipif(not ocr_available(), reason="tesseract binary not on PATH")
def test_live_ocr_text_is_redacted_before_the_planner(monkeypatch):
    """End to end: screenshot -> tesseract -> redactor -> prompt.
    No secret from the pixels may reach the planner prompt."""
    monkeypatch.setattr(app_config, "ALLOW_SCREENSHOTS", True, raising=False)
    monkeypatch.setattr(app_config, "OCR_ENABLED", True, raising=False)

    shot = _render_secret_png()
    obs = BrowserObservation.model_validate({
        "tabId": 1, "url": "http://test.local/page", "title": "Test page",
        "interactive": [],
        "scroll": {"y": 0, "viewport": {"w": 900, "h": 600}}, "dpr": 1,
        "pageText": "Ordinary page content.",
        "screenshot": shot,
    })
    payload, _ = classify_and_redact(obs, instruction="find headphones")
    assert payload.ocr_text, "OCR text should reach the payload (then be redacted)"
    assert "[REDACTED]" in payload.ocr_text
    prompt = build_prompt("find headphones", payload, [])
    assert OTP not in prompt, "labelled OTP from OCR pixels reached the prompt"
    assert PASSWORD not in prompt, "bare password from OCR pixels reached the prompt"
    assert "Baker Street" not in prompt, "postal address from OCR pixels reached the prompt"
