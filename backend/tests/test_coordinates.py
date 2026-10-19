import pytest

from app.observation import ground_coords
from app.privacy import PrivacyFilteredPayload, classify_and_redact


def test_dpr2_screenshot_point_maps_to_css_page_px():
    p = ground_coords(device_x=400, device_y=800, dpr=2, scroll_y=300)
    assert (p.x, p.y) == (200.0, 700.0)


def test_dpr_zero_rejected():
    with pytest.raises(ValueError):
        ground_coords(device_x=1, device_y=1, dpr=0, scroll_y=0)


def test_payload_context_block_hides_redacted_values():
    from app.contracts import BrowserObservation, InteractiveElement
    obs = BrowserObservation.model_validate({
        "tabId": 1, "url": "http://t/", "title": "T",
        "interactive": [
            InteractiveElement.model_validate({"targetId": "pw", "role": "textbox", "name": "Password", "tag": "input", "inputType": "password", "value": "hunter2", "visible": True, "enabled": True}),
            InteractiveElement.model_validate({"targetId": "q", "role": "textbox", "name": "Search", "tag": "input", "inputType": "text", "value": "jwst", "visible": True, "enabled": True}),
        ],
        "scroll": {}, "dpr": 1,
    })
    payload, _ = classify_and_redact(obs, instruction="search")
    block = payload.to_context_block()
    assert "hunter2" not in block
    assert 'value="jwst"' in block
