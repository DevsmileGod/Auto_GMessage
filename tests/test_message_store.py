"""Tests for the first-message pool, 24h cooldown, and persistence."""

import pytest

from message_store import COOLDOWN_SECONDS, FirstMessage, MessageStore


@pytest.fixture
def store(tmp_path):
    return MessageStore(tmp_path / "messages.json")


# ------------------------------------------------------------------- CRUD


def test_add_edit_delete_first(store):
    m = store.add_first("Subject A", "Body A")
    assert [x.subject for x in store.first_pool] == ["Subject A"]

    assert store.update_first(m.id, "Subject B", "Body B")
    assert store.get_first(m.id).subject == "Subject B"

    assert store.delete_first(m.id)
    assert store.first_pool == []


def test_update_and_delete_unknown_id_return_false(store):
    assert not store.update_first("nope", "s", "b")
    assert not store.delete_first("nope")


def test_second_message_set_and_clear(store):
    store.set_second("  Reply body  ")
    assert store.second_body == "Reply body"
    store.clear_second()
    assert store.second_body == ""


def test_persistence_round_trip(tmp_path):
    path = tmp_path / "messages.json"
    store = MessageStore(path)
    store.add_first("S1", "B1")
    store.add_first("S2", "B2")
    store.set_second("Reply")

    reloaded = MessageStore(path)
    assert [m.subject for m in reloaded.first_pool] == ["S1", "S2"]
    assert reloaded.second_body == "Reply"


def test_ids_are_stable_across_reload(tmp_path):
    path = tmp_path / "messages.json"
    store = MessageStore(path)
    m = store.add_first("S", "B")
    reloaded = MessageStore(path)
    assert reloaded.first_pool[0].id == m.id


def test_corrupt_file_loads_as_empty(tmp_path):
    path = tmp_path / "messages.json"
    path.write_text("{not json", encoding="utf-8")
    store = MessageStore(path)
    assert store.first_pool == []
    assert store.second_body == ""


# --------------------------------------------------------------- cooldown


def test_new_message_is_available():
    m = FirstMessage(subject="S", body="B")
    assert m.is_available()
    assert m.cooldown_remaining() == 0


def test_sent_message_is_locked_for_24h():
    now = 1_000_000.0
    m = FirstMessage(subject="S", body="B")
    m.mark_sent(now=now)

    assert not m.is_available(now=now)
    assert m.cooldown_remaining(now=now) == pytest.approx(COOLDOWN_SECONDS)
    # one hour later, still locked
    assert not m.is_available(now=now + 3600)
    # just before 24h, still locked; just after, available
    assert not m.is_available(now=now + COOLDOWN_SECONDS - 1)
    assert m.is_available(now=now + COOLDOWN_SECONDS + 1)


def test_available_first_excludes_locked_and_empty():
    now = 1_000_000.0
    store = MessageStore.__new__(MessageStore)  # no file
    store._path = None
    store.first_pool = [
        FirstMessage(subject="ready", body="b", id="1"),
        FirstMessage(subject="locked", body="b", id="2", last_sent_at=now),
        FirstMessage(subject="", body="b", id="3"),        # empty subject
        FirstMessage(subject="also ready", body="b", id="4"),
    ]
    store.second_body = ""
    available = store.available_first(now=now)
    assert [m.id for m in available] == ["1", "4"]
    assert store.available_count(now=now) == 2


def test_reset_cooldowns_unlocks_everything(tmp_path):
    store = MessageStore(tmp_path / "m.json")
    m = store.add_first("S", "B")
    m.mark_sent()
    store.save()
    assert not m.is_available()

    store.reset_cooldowns()
    assert store.first_pool[0].is_available()
    assert MessageStore(tmp_path / "m.json").first_pool[0].is_available()


def test_cooldown_survives_reload(tmp_path):
    path = tmp_path / "m.json"
    store = MessageStore(path)
    m = store.add_first("S", "B")
    m.mark_sent()
    store.save()

    reloaded = MessageStore(path)
    assert not reloaded.first_pool[0].is_available()
    assert reloaded.available_count() == 0
