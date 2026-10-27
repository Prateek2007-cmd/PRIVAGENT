"""Test SQLite persistence, session isolation, and chat memory."""
import os
import tempfile
import pytest

from app import db


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init_db(path)
    yield path
    try:
        os.remove(path)
    except OSError:
        pass


def test_init_and_create_chat(temp_db):
    chat = db.create_chat(title="Test Mission", db_path=temp_db)
    assert chat["id"] is not None
    assert chat["title"] == "Test Mission"
    assert chat["status"] == "active"
    assert chat["messages"] == []
    assert chat["memory"] == {}


def test_list_and_delete_chat(temp_db):
    c1 = db.create_chat(title="Chat 1", db_path=temp_db)
    c2 = db.create_chat(title="Chat 2", db_path=temp_db)
    chats = db.list_chats(db_path=temp_db)
    assert len(chats) == 2
    assert {c["id"] for c in chats} == {c1["id"], c2["id"]}

    deleted = db.delete_chat(c1["id"], db_path=temp_db)
    assert deleted is True
    assert db.get_chat(c1["id"], db_path=temp_db) is None
    assert len(db.list_chats(db_path=temp_db)) == 1


def test_messages_persistence(temp_db):
    chat = db.create_chat(title="Chat With Messages", db_path=temp_db)
    cid = chat["id"]

    msg1 = db.add_message(cid, "user", "Hello agent", db_path=temp_db)
    msg2 = db.add_message(cid, "agent", "Hello user", evidence="navigated to google", db_path=temp_db)

    msgs = db.get_messages(cid, db_path=temp_db)
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "Hello agent"
    assert msgs[1]["role"] == "agent"
    assert msgs[1]["evidence"] == "navigated to google"


def test_chat_memory_and_isolation(temp_db):
    c1 = db.create_chat(title="Session 1", db_path=temp_db)
    c2 = db.create_chat(title="Session 2", db_path=temp_db)

    db.set_chat_memory(c1["id"], "selected_product", "Realme 15 Pro", db_path=temp_db)
    db.set_chat_memory(c1["id"], "price", "₹24,999", db_path=temp_db)

    db.set_chat_memory(c2["id"], "selected_product", "Yellow Dress", db_path=temp_db)

    mem1 = db.get_chat_memory(c1["id"], db_path=temp_db)
    mem2 = db.get_chat_memory(c2["id"], db_path=temp_db)

    # Strict isolation: Session 1 does not see Session 2 memory
    assert mem1["selected_product"] == "Realme 15 Pro"
    assert mem1["price"] == "₹24,999"

    assert mem2["selected_product"] == "Yellow Dress"
    assert "price" not in mem2
