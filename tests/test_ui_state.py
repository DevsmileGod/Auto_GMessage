"""UI state tests, focused on the paste box never being stranded read-only."""

import tkinter as tk

import pytest
from conftest import FakeGmailClient

import ui
from gmail_client import SendResult
from sender import EmailSender

ALICE = "alice@example.com"


@pytest.fixture
def app(tk_root, tmp_path, monkeypatch):
    """A real app window, isolated from the user's config and from the network."""
    monkeypatch.setattr(ui, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(ui, "TEMPLATES_PATH", tmp_path / "templates.json")
    monkeypatch.setattr(ui, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(ui, "load_credentials", lambda: None)
    monkeypatch.setattr(ui.messagebox, "showinfo", lambda *a, **k: None)
    monkeypatch.setattr(ui.messagebox, "showwarning", lambda *a, **k: None)
    monkeypatch.setattr(ui.messagebox, "showerror", lambda *a, **k: None)
    monkeypatch.setattr(ui.messagebox, "askyesno", lambda *a, **k: False)

    window = tk.Toplevel(tk_root)
    window.withdraw()
    application = ui.GmailAutoSenderApp(root=window)
    yield application
    application._closing = True
    window.destroy()


def paste_state(application) -> str:
    return str(application.paste_text.cget("state"))


def drain(application) -> None:
    """Run everything the worker thread queued for the Tk thread."""
    application._pump()


# ------------------------------------------------------------- the bug #3


def test_paste_box_starts_editable(app):
    assert paste_state(app) == tk.NORMAL


def test_paste_box_is_editable_again_after_import(app):
    app.paste_text.insert("1.0", f"{ALICE}\nbob@example.com\n")
    app._import_recipients_from_paste()

    assert paste_state(app) == tk.NORMAL
    assert app.paste_text.get("1.0", tk.END).strip() == ""

    # And it genuinely accepts new text.
    app.paste_text.insert("1.0", "carol@example.com")
    assert app.paste_text.get("1.0", tk.END).strip() == "carol@example.com"


def test_second_import_after_first_still_works(app):
    app.paste_text.insert("1.0", ALICE)
    app._import_recipients_from_paste()
    app.paste_text.insert("1.0", "bob@example.com")
    app._import_recipients_from_paste()

    assert sorted(app._get_recipient_emails()) == ["alice@example.com", "bob@example.com"]
    assert paste_state(app) == tk.NORMAL


def test_paste_box_is_disabled_while_sending_and_restored_when_complete(app):
    app._update_control_states("sending")
    assert paste_state(app) == tk.DISABLED

    app._on_send_complete(stopped=False, _results=[], failed_emails=[])
    drain(app)

    assert paste_state(app) == tk.NORMAL


def test_paste_box_is_restored_after_a_stopped_send(app):
    app._update_control_states("sending")
    app._on_send_complete(stopped=True, _results=[], failed_emails=[])
    drain(app)

    assert paste_state(app) == tk.NORMAL


def test_paste_box_is_restored_when_retry_is_declined(app):
    """The original bug: the 'retry?' branch returned without re-enabling the box."""
    app._update_control_states("sending")
    app._on_send_complete(stopped=False, _results=[], failed_emails=[ALICE])
    drain(app)

    assert paste_state(app) == tk.NORMAL


def test_paste_box_is_restored_when_retry_cannot_start(app, monkeypatch):
    """Retry accepted, but sign-in fails — the box must still come back."""
    monkeypatch.setattr(ui.messagebox, "askyesno", lambda *a, **k: True)
    monkeypatch.setattr(app, "_ensure_login", lambda: False)

    app._update_control_states("sending")
    app._on_send_complete(stopped=False, _results=[], failed_emails=[ALICE])
    drain(app)

    assert paste_state(app) == tk.NORMAL


def test_completion_restores_controls_even_while_the_worker_thread_is_alive(app, monkeypatch):
    """on_complete fires from inside the worker, so is_running is still True there."""
    monkeypatch.setattr(ui.messagebox, "askyesno", lambda *a, **k: False)
    app._email_sender = EmailSender(FakeGmailClient(), interval_seconds=1)
    app._email_sender._thread = type("T", (), {"is_alive": lambda self: True})()
    assert app._is_sending()

    app._update_control_states("sending")
    app._on_send_complete(stopped=False, _results=[], failed_emails=[ALICE])
    drain(app)

    assert paste_state(app) == tk.NORMAL


def test_accepted_retry_keeps_the_controls_locked(app, monkeypatch):
    monkeypatch.setattr(ui.messagebox, "askyesno", lambda *a, **k: True)
    monkeypatch.setattr(app, "_ensure_login", lambda: True)
    # Two messages and a long interval, so the retry is still in flight when we look.
    app._email_sender = EmailSender(FakeGmailClient(), interval_seconds=30)
    app._email_sender._pending = {ALICE: [1, 2]}
    app._last_messages = [
        ui.Message("one", "body one"),
        ui.Message("two", "body two"),
    ]
    app._add_recipients([ALICE])

    app._update_control_states("sending")
    app._on_send_complete(stopped=False, _results=[], failed_emails=[ALICE])
    drain(app)

    assert paste_state(app) == tk.DISABLED, "a running retry must keep the box locked"
    app._email_sender.stop()
    app._email_sender._thread.join(timeout=5)


def test_import_buttons_disabled_while_sending_and_re_enabled_when_idle(app):
    buttons = (app.import_btn, app.csv_btn, app.delete_btn, app.clear_btn)

    app._update_control_states("sending")
    assert all(str(b.cget("state")) == tk.DISABLED for b in buttons)

    app._update_control_states("idle")
    assert all(str(b.cget("state")) == tk.NORMAL for b in buttons)


def test_import_is_refused_mid_send_rather_than_silently_dropped(app):
    app._email_sender = EmailSender(FakeGmailClient(), interval_seconds=1)
    app._email_sender._thread = type("T", (), {"is_alive": lambda self: True})()

    app.paste_text.insert("1.0", ALICE)
    app._import_recipients_from_paste()

    assert app._get_recipient_emails() == []


# ------------------------------------------------------- recipient rows


def test_row_shows_partial_until_both_messages_land(app):
    app._email_sender = EmailSender(FakeGmailClient(), interval_seconds=1)
    app._add_recipients([ALICE])
    app._email_sender._pending = {ALICE: [1, 2]}

    app._on_send_result(SendResult(email=ALICE, success=True, message_index=1), 1, 2)
    drain(app)
    app._email_sender._pending[ALICE] = [2]
    app._on_send_result(SendResult(email=ALICE, success=True, message_index=1), 1, 2)
    drain(app)
    assert app._recipient_records[ALICE]["status"] == ui.STATUS_PARTIAL
    assert app._recipient_records[ALICE]["sent"] is False

    app._email_sender._pending[ALICE] = []
    app._on_send_result(SendResult(email=ALICE, success=True, message_index=2), 2, 2)
    drain(app)
    assert app._recipient_records[ALICE]["status"] == ui.STATUS_SENT
    assert app._recipient_records[ALICE]["sent"] is True


def test_failed_row_is_marked_failed_and_not_sent(app):
    app._email_sender = EmailSender(FakeGmailClient(), interval_seconds=1)
    app._add_recipients([ALICE])

    app._on_send_result(
        SendResult(email=ALICE, success=False, message_index=1, error="nope"), 1, 2
    )
    drain(app)

    assert app._recipient_records[ALICE]["status"] == ui.STATUS_FAILED
    assert app._recipient_records[ALICE]["sent"] is False


def test_paste_parses_commas_and_angle_brackets(app):
    app.paste_text.insert("1.0", "alice@example.com, <bob@example.com>; carol@example.com")
    app._import_recipients_from_paste()

    assert sorted(app._get_recipient_emails()) == [
        "alice@example.com",
        "bob@example.com",
        "carol@example.com",
    ]


def test_legacy_single_message_template_loads_into_both_tabs(app, monkeypatch):
    """templates.json from the old one-message app must still open."""
    ui.TEMPLATES_PATH.write_text(
        '{"coll_1": {"subject": "Let us collaborate!", "body": "Hi, I am Oleg."}}',
        encoding="utf-8",
    )
    app._refresh_template_dropdown()
    app.template_var.set("coll_1")
    app._load_template()

    for editor in app._editors:
        message = editor.get_message()
        assert message.subject == "Let us collaborate!"
        assert message.body == "Hi, I am Oleg."


def test_two_message_template_round_trips(app, monkeypatch):
    monkeypatch.setattr(ui.simpledialog, "askstring", lambda *a, **k: "outreach")
    app._editors[0].set_message("One", "Body one")
    app._editors[1].set_message("Two", "Body two")
    app._save_template()

    app._editors[0].set_message("", "")
    app._editors[1].set_message("", "")
    app.template_var.set("outreach")
    app._load_template()

    assert app._editors[0].get_message() == ui.Message("One", "Body one")
    assert app._editors[1].get_message() == ui.Message("Two", "Body two")


def test_duplicate_import_adds_nothing(app):
    app.paste_text.insert("1.0", ALICE)
    app._import_recipients_from_paste()
    app.paste_text.insert("1.0", "ALICE@EXAMPLE.COM")
    app._import_recipients_from_paste()

    assert app._get_recipient_emails() == [ALICE]
    assert paste_state(app) == tk.NORMAL
