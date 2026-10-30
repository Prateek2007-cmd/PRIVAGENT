"""Test REST API routes for multi-turn chats, session isolation, and messages."""
from fastapi.testclient import TestClient
from app.main import app
from app import db


def test_chat_lifecycle_api():
    client = TestClient(app)

    # 1. Create a new chat
    res = client.post("/api/chat", json={"title": "Custom Research Mission"})
    assert res.status_code == 200
    data = res.json()
    chat_id = data["chatId"]
    assert chat_id is not None
    assert data["title"] == "Custom Research Mission"

    # 2. Get chat details
    res = client.get(f"/api/chat/{chat_id}")
    assert res.status_code == 200
    chat_data = res.json()
    assert chat_data["id"] == chat_id
    assert chat_data["title"] == "Custom Research Mission"
    assert chat_data["messages"] == []

    # 3. List chats
    res = client.get("/api/chats")
    assert res.status_code == 200
    chats = res.json()
    assert any(c["id"] == chat_id for c in chats)

    # 4. Post message to chat
    res = client.post(f"/api/chat/{chat_id}/message", json={"instruction": "Search for Hubble telescope"})
    assert res.status_code == 200
    msg_data = res.json()
    assert msg_data["chatId"] == chat_id
    assert msg_data["taskId"] is not None

    # 5. Delete chat
    res = client.delete(f"/api/chat/{chat_id}")
    assert res.status_code == 200
    assert res.json() == {"ok": True}

    # 6. Verify deleted
    res = client.get(f"/api/chat/{chat_id}")
    assert res.status_code == 404


def test_legacy_api_task_auto_creates_chat():
    client = TestClient(app)
    res = client.post("/api/task", json={"instruction": "Automated standalone task"})
    assert res.status_code == 200
    data = res.json()
    assert "taskId" in data
    assert "chatId" in data
