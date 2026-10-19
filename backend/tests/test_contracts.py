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
from app.main import app


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
