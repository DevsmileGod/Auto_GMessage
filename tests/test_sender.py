"""Tests for the two-messages-per-recipient send loop."""

import threading
import time

import pytest
from conftest import FakeGmailClient, run_to_completion

from sender import MAX_RETRY_ATTEMPTS, EmailSender, Message, deduplicate_emails, validate_send_inputs

ALICE = "alice@example.com"
BOB = "bob@example.com"

MESSAGES = [
    Message(subject="First", body="Hello, this is message one."),
    Message(subject="Second", body="Hello again, this is message two."),
]


def make_sender(client, interval=30):
    return EmailSender(client, interval_seconds=interval)


def collect(email_sender, emails, messages=None, retry=False):
    """Run a send and return (results, completion payload)."""
    results = []
    completion = {}
    done = threading.Event()

    def on_complete(stopped, all_results, retryable):
        completion.update(stopped=stopped, results=all_results, retryable=retryable)
        done.set()

    starter = email_sender.start_retry if retry else email_sender.start
    started = starter(
        emails=emails,
        messages=messages or MESSAGES,
        on_result=lambda r, cur, tot: results.append((r, cur, tot)),
        on_complete=on_complete,
    )
    assert started, "sender refused to start"
    run_to_completion(email_sender)
    assert done.wait(timeout=2), "on_complete never fired"
    return results, completion


# --------------------------------------------------------------------- order


def test_sends_both_messages_to_each_recipient_in_order(no_sleep):
    client = FakeGmailClient()
    email_sender = make_sender(client)

    collect(email_sender, [ALICE, BOB])

    assert client.sent == [(ALICE, 1), (ALICE, 2), (BOB, 1), (BOB, 2)]


def test_waits_between_every_message_but_not_after_the_last(no_sleep):
    client = FakeGmailClient()
    email_sender = make_sender(client, interval=45)

    collect(email_sender, [ALICE, BOB])

    # 4 messages => 3 gaps: alice1→alice2, alice2→bob1, bob1→bob2.
    assert no_sleep == [45, 45, 45]


def test_single_recipient_waits_once_between_the_two_messages(no_sleep):
    client = FakeGmailClient()
    email_sender = make_sender(client, interval=10)

    collect(email_sender, [ALICE])

    assert client.sent == [(ALICE, 1), (ALICE, 2)]
    assert no_sleep == [10]


def test_interval_is_actually_honored_in_wall_clock():
    """The real _sleep_interval is used here — no monkeypatch."""
    client = FakeGmailClient()
    email_sender = make_sender(client, interval=1)

    start = time.monotonic()
    collect(email_sender, [ALICE])
    elapsed = time.monotonic() - start

    assert client.sent == [(ALICE, 1), (ALICE, 2)]
    assert elapsed >= 1.0, f"second message went out after only {elapsed:.2f}s"


def test_progress_counts_every_message_not_every_recipient(no_sleep):
    client = FakeGmailClient()
    email_sender = make_sender(client)

    results, _ = collect(email_sender, [ALICE, BOB])

    totals = {total for _, _, total in results}
    currents = [current for _, current, _ in results]
    assert totals == {4}
    assert currents == [1, 2, 3, 4]


# ------------------------------------------------------------------ failures


def test_failed_first_message_skips_the_second_for_that_recipient(no_sleep):
    client = FakeGmailClient(fail_on={(ALICE, 1)})
    email_sender = make_sender(client)

    _, completion = collect(email_sender, [ALICE, BOB])

    assert client.sent == [(ALICE, 1), (BOB, 1), (BOB, 2)]
    assert (ALICE, 2) not in client.sent
    assert completion["retryable"] == [ALICE]
    assert email_sender.pending_messages(ALICE) == [1, 2]


def test_failure_does_not_skip_the_interval_before_the_next_recipient(no_sleep):
    client = FakeGmailClient(fail_on={(ALICE, 1)})
    email_sender = make_sender(client, interval=20)

    collect(email_sender, [ALICE, BOB])

    # alice1(fail) → wait → bob1 → wait → bob2. Two gaps, none after bob2.
    assert no_sleep == [20, 20]


def test_failed_second_message_leaves_only_that_message_pending(no_sleep):
    client = FakeGmailClient(fail_on={(ALICE, 2)})
    email_sender = make_sender(client)

    _, completion = collect(email_sender, [ALICE])

    assert client.sent == [(ALICE, 1), (ALICE, 2)]
    assert completion["retryable"] == [ALICE]
    assert email_sender.pending_messages(ALICE) == [2]


def test_successful_recipient_has_nothing_pending(no_sleep):
    client = FakeGmailClient()
    email_sender = make_sender(client)

    _, completion = collect(email_sender, [ALICE, BOB])

    assert completion["retryable"] == []
    assert email_sender.pending_messages(ALICE) == []
    assert email_sender.pending_messages(BOB) == []


# -------------------------------------------------------------------- retry


def test_retry_resends_only_the_undelivered_message(no_sleep):
    client = FakeGmailClient(fail_on={(ALICE, 2)})
    email_sender = make_sender(client)
    collect(email_sender, [ALICE])

    client.fail_on.clear()
    client.sent.clear()
    _, completion = collect(email_sender, [ALICE], retry=True)

    # Message 1 already landed; retrying must not deliver it a second time.
    assert client.sent == [(ALICE, 2)]
    assert completion["retryable"] == []
    assert email_sender.pending_messages(ALICE) == []


def test_retry_after_first_message_failure_resends_both(no_sleep):
    client = FakeGmailClient(fail_on={(ALICE, 1)})
    email_sender = make_sender(client)
    collect(email_sender, [ALICE])

    client.fail_on.clear()
    client.sent.clear()
    collect(email_sender, [ALICE], retry=True)

    assert client.sent == [(ALICE, 1), (ALICE, 2)]


def test_recipient_stops_being_retryable_after_max_attempts(no_sleep):
    client = FakeGmailClient(fail_on={(ALICE, 1)})
    email_sender = make_sender(client)
    collect(email_sender, [ALICE])

    for _ in range(MAX_RETRY_ATTEMPTS):
        _, completion = collect(email_sender, [ALICE], retry=True)

    assert completion["retryable"] == []
    assert email_sender.pending_messages(ALICE) == [1, 2]


def test_new_send_clears_retry_state_from_the_previous_one(no_sleep):
    client = FakeGmailClient(fail_on={(ALICE, 1)})
    email_sender = make_sender(client)
    collect(email_sender, [ALICE])
    assert email_sender.get_retryable_failed() == [ALICE]

    client.fail_on.clear()
    collect(email_sender, [ALICE])

    assert email_sender.get_retryable_failed() == []


# ------------------------------------------------------------ stop and pause


def test_stop_halts_before_the_next_message(no_sleep):
    client = FakeGmailClient()
    email_sender = make_sender(client)
    done = threading.Event()
    completion = {}

    def on_result(result, current, total):
        email_sender.stop()

    def on_complete(stopped, results, retryable):
        completion.update(stopped=stopped, retryable=retryable)
        done.set()

    email_sender.start(
        emails=[ALICE, BOB], messages=MESSAGES, on_result=on_result, on_complete=on_complete
    )
    run_to_completion(email_sender)
    assert done.wait(timeout=2)

    assert client.sent == [(ALICE, 1)]
    assert completion["stopped"] is True
    assert completion["retryable"] == []


def test_pause_freezes_the_loop_until_resumed():
    client = FakeGmailClient()
    email_sender = make_sender(client, interval=1)
    done = threading.Event()

    email_sender.start(
        emails=[ALICE],
        messages=MESSAGES,
        on_complete=lambda *_: done.set(),
    )
    email_sender.pause()
    time.sleep(0.4)
    assert email_sender.is_paused
    sent_while_paused = len(client.sent)

    email_sender.resume()
    run_to_completion(email_sender)
    assert done.wait(timeout=3)

    assert sent_while_paused <= 1
    assert client.sent == [(ALICE, 1), (ALICE, 2)]


def test_cannot_start_a_second_send_while_one_is_running():
    client = FakeGmailClient()
    email_sender = make_sender(client, interval=1)

    assert email_sender.start(emails=[ALICE], messages=MESSAGES)
    assert not email_sender.start(emails=[BOB], messages=MESSAGES)

    email_sender.stop()
    run_to_completion(email_sender)


def test_send_refuses_when_not_logged_in(no_sleep):
    client = FakeGmailClient()
    client.is_logged_in = lambda: False
    email_sender = make_sender(client)

    assert not email_sender.start(emails=[ALICE], messages=MESSAGES)
    assert client.sent == []


# --------------------------------------------------------------- validation


def test_duplicate_recipients_receive_the_messages_once(no_sleep):
    client = FakeGmailClient()
    email_sender = make_sender(client)

    collect(email_sender, [ALICE, "ALICE@example.com", ALICE])

    assert client.sent == [(ALICE, 1), (ALICE, 2)]


def test_deduplicate_emails_is_case_insensitive():
    unique, removed = deduplicate_emails([ALICE, "Alice@Example.com", BOB, " "])
    assert unique == [ALICE, BOB]
    assert removed == 1


@pytest.mark.parametrize(
    "emails, messages, expected_fragment",
    [
        ([], MESSAGES, "Email list is empty"),
        (["not-an-email"], MESSAGES, "Invalid address"),
        ([ALICE], [MESSAGES[0]], "Exactly 2 messages"),
        ([ALICE], [Message("", "body"), MESSAGES[1]], "Message 1: subject is required"),
        ([ALICE], [MESSAGES[0], Message("subject", "  ")], "Message 2: body is empty"),
    ],
)
def test_validation_rejects_bad_input(emails, messages, expected_fragment):
    errors = validate_send_inputs(emails, messages)
    assert any(expected_fragment in error for error in errors), errors


def test_validation_accepts_a_good_request():
    assert validate_send_inputs([ALICE, BOB], MESSAGES) == []


def test_sender_refuses_to_start_on_invalid_input(no_sleep):
    client = FakeGmailClient()
    email_sender = make_sender(client)

    assert not email_sender.start(emails=[ALICE], messages=[Message("", "")] * 2)
    assert client.sent == []
