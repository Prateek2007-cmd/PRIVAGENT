"""Contract parity: the TS source of truth and this mirror must not drift."""
import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.contracts import (
    ACTION_TYPES,
    RISK_CLASSES,
    TASK_STATES,
    BrowserAction,
    BrowserObservation,
    InteractiveElement,
    TaskProgress,
    UserTask,
)
from app import agentLoop
from app.hub import TaskRecord
from app.main import app
from app.privacy import classify_and_redact


def read_action_types_from_ts() -> list[str]:
    ts = Path(__file__).parents[2] / "extension" / "src" / "contracts.ts"
    text = ts.read_text(encoding="utf-8")
    m = re.search(r"export const ACTION_TYPES = \[(.*?)\] as const", text, re.S)
    assert m, "ACTION_TYPES block not found in contracts.ts"
    return [s.strip().strip('"').strip("'") for s in m.group(1).split(",") if s.strip()]


def test_action_types_match():
    assert ACTION_TYPES == read_action_types_from_ts()


def test_risk_classes_match():
    ts = Path(__file__).parents[2] / "extension" / "src" / "contracts.ts"
    m = re.search(r'export const RISK_CLASSES = \[(.*?)\] as const', ts.read_text(encoding="utf-8"), re.S)
    assert RISK_CLASSES == [s.strip().strip('"') for s in m.group(1).split(",") if s.strip()]


def test_roundtrip_observation():
    obs = BrowserObservation.model_validate({
        "tabId": 3, "url": "http://x/", "title": "t",
        "interactive": [{"targetId": "b1", "role": "button", "name": "Search", "tag": "button", "visible": True, "enabled": True}],
        "scroll": {"y": 0, "viewport": {"w": 900, "h": 600}}, "dpr": 1,
    })
    assert obs.interactive[0].target_id == "b1"


def test_roundtrip_action_camelcase():
    a = BrowserAction.model_validate({
        "type": "click", "risk": "write", "verify": "changed",
        "timeoutMs": 5000, "target": {"targetId": "b1"},
    })
    assert a.timeout_ms == 5000


def _assert_no_nulls(value):
    if isinstance(value, dict):
        for key, entry in value.items():
            assert entry is not None, f"unexpected null at {key!r}"
            _assert_no_nulls(entry)
    elif isinstance(value, list):
        for entry in value:
            _assert_no_nulls(entry)


def test_real_planner_omits_unset_optional_params(monkeypatch):
    """The production WebSocket payload must not carry JSON nulls.

    Pydantic's default model_dump() emits every unset ActionParams field as
    null; the extension treats null and undefined as absent, but the wire
    format should stay minimal and unambiguous.
    """
    class FakeProvider:
        def plan_call(self, instruction, payload, history, working_memory=None):
            return {
                "goal": "open flipkart",
                "steps": [{
                    "why": "open the storefront",
                    "action": {
                        "type": "navigate",
                        "risk": "write",
                        "verify": "storefront loaded",
                        "timeoutMs": 5000,
                        "params": {"url": "https://www.flipkart.com/"},
                    },
                }],
            }

    monkeypatch.setattr(agentLoop, "_provider_for_config", lambda: FakeProvider())
    record = TaskRecord(task_id="wire-test", instruction="open flipkart")
    planner = agentLoop._real_planner(record)

    obs = BrowserObservation.model_validate({
        "tabId": 1, "url": "https://example.com", "title": "Example",
        "interactive": [], "scroll": {}, "dpr": 1,
    })
    payload, _ = classify_and_redact(obs, instruction="open flipkart")
    plan = planner("open flipkart", payload, [])

    action = plan["steps"][0]["action"]
    assert action["params"] == {"url": "https://www.flipkart.com/"}
    assert "target" not in action
    _assert_no_nulls(plan)


def test_task_progress_alias():
    p = TaskProgress.model_validate({"taskId": "t1", "taskState": "running", "stepsDone": 2, "message": "hi"})
    assert p.task_id == "t1"


def test_user_task_default_state():
    t = UserTask(id="abc", instruction="do a thing")
    assert t.status == "planning"


def test_api_task_create_and_get():
    c = TestClient(app)
    r = c.post("/api/task", json={"instruction": "probe"})
    assert r.status_code == 200
    tid = r.json()["taskId"]
    g = c.get(f"/api/task/{tid}")
    assert g.status_code == 200
    assert g.json()["taskState"] in TASK_STATES
