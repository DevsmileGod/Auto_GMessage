"""UI tests: message CRUD, recipient queue, control states, campaign events, resume."""

import tkinter as tk

import pytest

import campaign_state
import message_store
import ui
from gmail_client import SendResult

ALICE = "alice@example.com"
BOB = "bob@example.com"


class FakeFirstDialog:
    next_result = None

    def __init__(self, parent, subject="", body="", title=""):
        self.result = FakeFirstDialog.next_result


@pytest.fixture
def app(tk_root, tmp_path, monkeypatch):
    monkeypatch.setattr(ui, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(ui, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(message_store, "MESSAGES_PATH", tmp_path / "messages.json")
    monkeypatch.setattr(campaign_state, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(ui, "load_credentials", lambda: None)
    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(ui.messagebox, name, lambda *a, **k: None)
    monkeypatch.setattr(ui.messagebox, "askyesno", lambda *a, **k: True)
    monkeypatch.setattr(ui, "FirstMessageDialog", FakeFirstDialog)

    window = tk.Toplevel(tk_root)
    window.withdraw()
    application = ui.GmailAutoSenderApp(root=window)
    yield application
    application._closing = True
    window.destroy()


def paste_state(app) -> str:
    return str(app.paste_text.cget("state"))


def drain(app) -> None:
    app._pump()


# -------------------------------------------------- first-message CRUD


def test_new_edit_delete_first_message(app):
    FakeFirstDialog.next_result = ("Subject A", "Body A")
    app._new_first()
    assert [m.subject for m in app._store.first_pool] == ["Subject A"]

    mid = app._store.first_pool[0].id
    app.first_tree.selection_set(mid)
    FakeFirstDialog.next_result = ("Edited", "New body")
    app._edit_first()
    assert app._store.get_first(mid).subject == "Edited"

    app.first_tree.selection_set(mid)
    app._delete_first()
    assert app._store.first_pool == []


def test_first_list_shows_lock_state(app):
    FakeFirstDialog.next_result = ("S", "B")
    app._new_first()
    app._store.first_pool[0].mark_sent()
    app._store.save()
    app._refresh_first_list()
    mid = app._store.first_pool[0].id
    assert app.first_tree.set(mid, "state").startswith("Locked")


def test_reset_locks(app):
    FakeFirstDialog.next_result = ("S", "B")
    app._new_first()
    app._store.first_pool[0].mark_sent()
    app._store.save()
    app._reset_locks()
    assert app._store.first_pool[0].is_available()


# ------------------------------------------------------ second message


def test_save_and_clear_second(app):
    app.second_text.insert("1.0", "This is my reply.")
    app._save_second()
    assert message_store.MessageStore(message_store.MESSAGES_PATH).second_body == "This is my reply."
    app._clear_second()
    assert app._store.second_body == ""


# ------------------------------------------------- recipients / paste box


def test_import_populates_state_and_clears_paste(app):
    app.paste_text.insert("1.0", f"{ALICE}\n{BOB}\n")
    app._import_recipients_from_paste()
    assert sorted(app._get_recipient_emails()) == [ALICE, BOB]
    assert app.paste_text.get("1.0", tk.END).strip() == ""
    assert paste_state(app) == tk.NORMAL
    # persisted to the state file
    assert sorted(campaign_state.CampaignState(campaign_state.STATE_PATH).emails()) == [ALICE, BOB]


def test_import_parses_separators(app):
    app.paste_text.insert("1.0", "alice@example.com, <bob@example.com>; carol@example.com")
    app._import_recipients_from_paste()
    assert sorted(app._get_recipient_emails()) == [
        "alice@example.com", "bob@example.com", "carol@example.com"
    ]


def test_duplicate_import_adds_nothing(app):
    app.paste_text.insert("1.0", ALICE)
    app._import_recipients_from_paste()
    app.paste_text.insert("1.0", "ALICE@EXAMPLE.COM")
    app._import_recipients_from_paste()
    assert app._get_recipient_emails() == [ALICE]


def test_clear_all_resets_state(app):
    app.paste_text.insert("1.0", f"{ALICE}\n{BOB}")
    app._import_recipients_from_paste()
    app._clear_recipients()
    assert app._get_recipient_emails() == []
    assert app._state.active is False


# --------------------------------------------------------- control states


def test_controls_lock_while_sending_and_restore_when_idle(app):
    editing = (
        app.import_btn, app.csv_btn, app.delete_btn, app.clear_btn,
        app.first_new_btn, app.first_edit_btn, app.first_del_btn,
        app.first_reset_btn, app.second_save_btn, app.second_clear_btn,
    )
    app._update_control_states("sending")
    assert paste_state(app) == tk.DISABLED
    assert all(str(b.cget("state")) == tk.DISABLED for b in editing)
    assert str(app.second_text.cget("state")) == tk.DISABLED

    app._update_control_states("idle")
    assert paste_state(app) == tk.NORMAL
    assert all(str(b.cget("state")) == tk.NORMAL for b in editing)


def test_paste_box_restored_after_complete(app):
    app._update_control_states("sending")
    app._ev_complete(stopped=False)
    drain(app)
    assert paste_state(app) == tk.NORMAL


def test_paste_box_restored_after_stop(app):
    app._update_control_states("sending")
    app._ev_complete(stopped=True)
    drain(app)
    assert paste_state(app) == tk.NORMAL


# ------------------------------------------------------- campaign events


def test_events_walk_row_through_lifecycle(app):
    app._add_recipients([ALICE])

    app._ev_first_sending(ALICE)
    drain(app)
    assert app.recipients_tree.set(ALICE.lower(), "status") == ui.STATUS_SENDING

    app._ev_first_result(SendResult(email=ALICE, success=True, message_index=1), 1, 1)
    drain(app)
    assert app.recipients_tree.set(ALICE.lower(), "status") == ui.STATUS_SENT

    app._ev_reply(ALICE)
    drain(app)
    assert app.recipients_tree.set(ALICE.lower(), "status") == ui.STATUS_REPLIED

    app._ev_second_result(SendResult(email=ALICE, success=True, message_index=2))
    drain(app)
    assert app.recipients_tree.set(ALICE.lower(), "status") == ui.STATUS_DONE


def test_failed_first_send_marks_row_failed(app):
    app._add_recipients([ALICE])
    app._ev_first_result(SendResult(email=ALICE, success=False, message_index=1, error="nope"), 1, 1)
    drain(app)
    assert app.recipients_tree.set(ALICE.lower(), "status") == ui.STATUS_FAILED


def test_waiting_event_updates_status(app):
    app._add_recipients([ALICE, BOB])
    app._ev_waiting(3600, 1, 2)
    drain(app)
    status = app.status_var.get()
    assert "Waiting" in status and "1/2" in status


# --------------------------------------------------- resume / start button


def test_start_button_says_resume_when_work_is_pending(app):
    app._add_recipients([ALICE, BOB])
    app._state.begin()          # a campaign is now active
    app._state.mark_sent(ALICE, "<id>", "S", 1.0)  # ALICE awaiting reply
    app._refresh_start_button()
    assert app.send_btn.cget("text") == "Resume campaign"


def test_start_button_says_start_when_idle(app):
    app._add_recipients([ALICE])
    app._refresh_start_button()
    assert app.send_btn.cget("text") == "Start campaign"


def test_launch_restores_recipients_and_statuses_from_disk(app, tk_root, tmp_path, monkeypatch):
    """A second app instance sees the saved queue, cursor, and statuses."""
    app._add_recipients([ALICE, BOB])
    app._state.begin()
    app._state.mark_sent(ALICE, "<id>", "Subj", 1.0)
    app._state.advance_cursor()  # cursor now points at BOB

    window = tk.Toplevel(tk_root)
    window.withdraw()
    reopened = ui.GmailAutoSenderApp(root=window)
    try:
        assert reopened._get_recipient_emails() == [ALICE, BOB]
        assert reopened._state.cursor == 1
        assert reopened._state.get(ALICE).status == campaign_state.STATUS_SENT
        assert reopened.send_btn.cget("text") == "Resume campaign"
        # the ▸ resume marker sits on BOB (index 1)
        assert reopened.recipients_tree.set(BOB.lower(), "num").startswith("▸")
    finally:
        reopened._closing = True
        window.destroy()


def test_start_blocked_without_messages(app, monkeypatch):
    warned = {}
    monkeypatch.setattr(ui.messagebox, "showwarning", lambda title, msg, *a, **k: warned.update(msg=msg))
    app._add_recipients([ALICE])
    app.start()
    assert app._campaign is None
    assert "first message" in warned.get("msg", "")
