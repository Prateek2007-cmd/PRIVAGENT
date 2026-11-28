"""Stream C (round 6): browser input verification — replace vs append.

Failing reproductions first (written before the fix). The backend
form-results guard (`_refill_differs_from_submitted`) compared a refill's
RAW value against previously submitted raw values. That misses the
replace/append distinction:

* replace-with-combined-value: the planner re-types the full combined
  value ("Hyderabad to Bengaluru") that was previously built by an
  append. Raw history holds {"Hyderabad", " to Bengaluru"}, so the guard
  calls it a refinement and the refill loops.
* fragment echo veto: the results-header echo check matched any
  SUBSTRING, so a genuine refinement to a shorter value ("Harbour")
  was vetoed because "Harbour" appears inside "Harbour City → ...",
  and an append fragment (" (BLR)") was vetoed because it appears
  inside the echoed header even though the combined value is new.

The contract now is: the guard reconstructs each field's submitted
COMBINED value (replace sets it, append extends it) and compares the
refill's EXPECTED FINAL value against those; the header echo check
matches a full header segment, not a substring.
"""
import pytest

from app.agentLoop import (
    _refill_differs_from_submitted,
    _value_echoed_in_results_header,
)

RESULTS_HEADER = "Harbour City → Lake Town · 2027-01-15"
RESULTS_TEXT = f"""### Results

{RESULTS_HEADER}

Coastal Line 204
Harbour Rail · Train
07:10 → 12:40
₹812
Select seats"""


def _type(text, name="From", target_id="from-input", clear=True, submit=False):
    params = {"text": text, "clear": clear, "submit": submit}
    return {
        "type": "type",
        "target": {"targetId": target_id, "name": name},
        "params": params,
    }


def _hist(*actions):
    return [{"result_ok": True, "action": a} for a in actions]


# ---------------------------------------------------------------------------
# _value_echoed_in_results_header — full segment, not substring
# ---------------------------------------------------------------------------

def test_echo_matches_full_header_segment():
    assert _value_echoed_in_results_header("Harbour City", RESULTS_TEXT) is True
    assert _value_echoed_in_results_header("Lake Town", RESULTS_TEXT) is True
    assert _value_echoed_in_results_header("2027-01-15", RESULTS_TEXT) is True


def test_echo_rejects_substring_of_a_segment():
    # "Harbour" is inside the echoed "Harbour City", but no submitted
    # value was ever just "Harbour" — a refinement to it is genuine.
    assert _value_echoed_in_results_header("Harbour", RESULTS_TEXT) is False
    assert _value_echoed_in_results_header("Lake", RESULTS_TEXT) is False
    assert _value_echoed_in_results_header("2027-01", RESULTS_TEXT) is False


def test_echo_ignores_short_values_and_listing_body():
    assert _value_echoed_in_results_header("HYD", RESULTS_TEXT) is False
    # A value that only appears in a result listing (past the header)
    # is not the submitted search.
    assert _value_echoed_in_results_header("Coastal Line 204", RESULTS_TEXT) is False
    assert _value_echoed_in_results_header("", RESULTS_TEXT) is False
    assert _value_echoed_in_results_header("Harbour City", None) is False


# ---------------------------------------------------------------------------
# _refill_differs_from_submitted — expected FINAL (combined) value
# ---------------------------------------------------------------------------

def test_replace_with_already_submitted_combined_value_is_repeat():
    # Field was built by replace + append; re-typing the full combined
    # value as a replace is a repeat, not a refinement.
    history = _hist(
        _type("Hyderabad", clear=True),
        _type(" to Bengaluru", clear=False),
    )
    action = _type("Hyderabad to Bengaluru", clear=True)
    assert _refill_differs_from_submitted(action, history, {}, None) is False


def test_replace_with_new_value_is_refinement():
    history = _hist(
        _type("Hyderabad", clear=True),
        _type(" to Bengaluru", clear=False),
    )
    action = _type("Chennai", clear=True)
    assert _refill_differs_from_submitted(action, history, {}, None) is True


def test_append_refinement_with_new_combined_value_executes():
    history = _hist(_type("Hyderabad", clear=True))
    action = _type(" to Bengaluru", clear=False)
    assert _refill_differs_from_submitted(action, history, {}, None) is True


def test_reappend_of_same_fragment_is_repeat():
    # The fragment was already appended and the field already ends with
    # it; appending it again only duplicates text — a loop, not a
    # refinement.
    history = _hist(
        _type("Hyderabad", clear=True),
        _type(" to Bengaluru", clear=False),
    )
    action = _type(" to Bengaluru", clear=False)
    assert _refill_differs_from_submitted(action, history, {}, None) is False


def test_append_fragment_echoed_in_header_but_combined_is_new_is_refinement():
    # Fragment " (BLR)" appears inside the echoed header, but the
    # combined value "Hyderabad (HYD) (BLR)" was never submitted.
    # The old substring echo check vetoed this genuine refinement.
    history = _hist(_type("Hyderabad (HYD)", clear=True))
    header = "Hyderabad (HYD) → Bengaluru (BLR) · 2026-12-05"
    action = _type(" (BLR)", clear=False)
    assert _refill_differs_from_submitted(action, history, {}, header) is True


def test_replace_with_shorter_value_echoed_as_substring_is_refinement():
    # Click-submitted search (no typed history); the header echoes
    # "Harbour City". Refining the From field down to "Harbour" is a
    # genuine new value, not a repeat of the submitted search.
    action = _type("Harbour", clear=True)
    assert _refill_differs_from_submitted(action, [], {}, RESULTS_TEXT) is True


def test_repeat_replace_after_click_submit_still_intercepted():
    # The regression the echo check exists for: same full value
    # re-typed after a Search click, no typed history.
    action = _type("Harbour City", clear=True)
    assert _refill_differs_from_submitted(action, [], {}, RESULTS_TEXT) is False


def test_exact_repeat_replace_from_history_is_repeat():
    history = _hist(_type("Harbour City", clear=True))
    action = _type("Harbour City", clear=True)
    assert _refill_differs_from_submitted(action, history, {}, None) is False


def test_date_refinement_still_executes():
    history = _hist(
        _type("2026-12-05", name="Travel date", target_id="date-input", clear=True, submit=True),
    )
    action = _type("2026-12-06", name="Travel date", target_id="date-input", clear=True)
    assert _refill_differs_from_submitted(action, history, {}, None) is True


def test_append_after_field_replaced_is_fresh_refinement():
    # The field was replaced since the fragment was last appended, so
    # appending it again builds a genuinely new combined value.
    history = _hist(
        _type("Hyderabad", clear=True),
        _type(" to Bengaluru", clear=False),
        _type("Chennai", clear=True),
    )
    action = _type(" to Bengaluru", clear=False)
    assert _refill_differs_from_submitted(action, history, {}, None) is True
