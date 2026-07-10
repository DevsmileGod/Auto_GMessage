"""Tests for the resumable two-phase Campaign."""

import threading

import pytest
from conftest import Collector, FakeGmailClient, FakeInbox, run_campaign

from campaign_state import CampaignState
from message_store import MessageStore
from sender import Campaign, CampaignCallbacks, deduplicate_emails, validate_campaign

A = "alice@example.com"
B = "bob@example.com"
C = "carol@example.com"
D = "dave@example.com"
E = "erin@example.com"


def setup(tmp_path, emails, subjects, second="Please see attached.", fresh=True):
    store = MessageStore(tmp_path / "messages.json")
    for subject in subjects:
        store.add_first(subject, f"Body of {subject}")
    if second is not None:
        store.set_second(second)
    state = CampaignState(tmp_path / "state.json")
    state.add_emails(emails)
    if fresh:
        state.begin()
    return store, state


@pytest.fixture
def no_sleep(monkeypatch):
    import sender

    waits: list[float] = []

    def fake_sleep(self, seconds):
        if self._stop_requested:
            return
        waits.append(seconds)

    monkeypatch.setattr(sender.Campaign, "_sleep", fake_sleep)
    return waits


def make(client, store, state, inbox, interval=30, poll=60):
    return Campaign(client, store, inbox, state, interval_seconds=interval, poll_interval_seconds=poll)


# --------------------------------------------------------- phase 1: rotation


def test_each_recipient_gets_a_distinct_message_in_order(tmp_path, no_sleep):
    store, state = setup(tmp_path, [A, B, C], ["M1", "M2", "M3"])
    client = FakeGmailClient()
    inbox = FakeInbox()
    for e in (A, B, C):
        inbox.add_reply(e)
    campaign = make(client, store, state, inbox)

    run_campaign(campaign, Collector())

    assert [(r.to, r.subject) for r in client.firsts()] == [(A, "M1"), (B, "M2"), (C, "M3")]


def test_cursor_flag_advances_and_persists(tmp_path, no_sleep):
    store, state = setup(tmp_path, [A, B], ["M1", "M2"])
    client = FakeGmailClient()
    inbox = FakeInbox()
    inbox.add_reply(A)
    inbox.add_reply(B)
    campaign = make(client, store, state, inbox)

    run_campaign(campaign, Collector())

    assert state.cursor == 2
    # reloaded from disk, the flag is preserved
    assert CampaignState(tmp_path / "state.json").cursor == 2


def test_sent_messages_are_locked_and_persist(tmp_path, no_sleep):
    store, state = setup(tmp_path, [A, B], ["M1", "M2"])
    client = FakeGmailClient()
    inbox = FakeInbox()
    inbox.add_reply(A)
    inbox.add_reply(B)
    run_campaign(make(client, store, state, inbox), Collector())

    assert store.available_count() == 0
    assert MessageStore(tmp_path / "messages.json").available_count() == 0


def test_failed_first_send_marks_failed_and_does_not_lock_a_message(tmp_path, no_sleep):
    store, state = setup(tmp_path, [A, B], ["M1", "M2"])
    client = FakeGmailClient(fail_first=[A])
    inbox = FakeInbox()
    inbox.add_reply(B)
    run_campaign(make(client, store, state, inbox), Collector())

    assert state.get(A).status == "Failed"
    # A's attempt did not consume a lock, so B drew the same first message.
    assert [r.subject for r in client.firsts()] == ["M1", "M1"]
    assert store.available_count() == 1  # M2 never used


# ------------------------------------------------------- batching + resume


def test_batch_stops_when_pool_exhausted_and_reports_wait(tmp_path, no_sleep):
    store, state = setup(tmp_path, [A, B, C], ["M1", "M2"])  # 2 messages, 3 recipients
    client = FakeGmailClient()
    inbox = FakeInbox()  # nobody replies → after batch, outreach incomplete → waits
    campaign = make(client, store, state, inbox)

    waited = []
    done = threading.Event()
    cb = CampaignCallbacks(
        on_waiting=lambda secs, contacted, total: (waited.append((secs, contacted, total)), campaign.stop()),
        on_complete=lambda stopped: done.set(),
    )
    campaign.start(cb)
    assert done.wait(timeout=10)
    campaign._thread.join(timeout=5)

    assert [r.to for r in client.firsts()] == [A, B]  # only the batch of two
    assert state.cursor == 2
    assert waited and waited[0][1] == 2 and waited[0][2] == 3
    assert waited[0][0] > 0  # a real wait was reported (~24h)
    assert state.active is True  # stopped mid-way → still resumable


def test_resume_after_unlock_continues_from_the_flag_and_watches_earlier_batches(tmp_path, no_sleep):
    # --- first run: contact A, B, then park (pool of 2 exhausted) ---
    store, state = setup(tmp_path, [A, B, C], ["M1", "M2"])
    client1 = FakeGmailClient()
    campaign1 = make(client1, store, state, FakeInbox())
    done1 = threading.Event()
    cb1 = CampaignCallbacks(
        on_waiting=lambda *_: campaign1.stop(),
        on_complete=lambda stopped: done1.set(),
    )
    campaign1.start(cb1)
    assert done1.wait(timeout=10)
    campaign1._thread.join(timeout=5)
    assert [r.to for r in client1.firsts()] == [A, B]
    assert state.cursor == 2

    # --- 24h passes (locks expire); app reopened → fresh objects, same files ---
    store.reset_cooldowns()
    store2 = MessageStore(tmp_path / "messages.json")
    state2 = CampaignState(tmp_path / "state.json")
    assert state2.cursor == 2  # resume flag survived
    assert state2.get(A).status == "Sent"  # A still awaiting a reply

    client2 = FakeGmailClient()
    inbox2 = FakeInbox()
    for e in (A, B, C):
        inbox2.add_reply(e)
    campaign2 = make(client2, store2, state2, inbox2)

    run_campaign(campaign2, Collector())

    # Only C is newly contacted on resume...
    assert [r.to for r in client2.firsts()] == [C]
    # ...but replies to the earlier batch (A, B) are still handled after resume.
    assert {r.to for r in client2.replies()} == {A, B, C}
    assert state2.is_finished()
    assert state2.active is False


def test_adding_recipients_reopens_a_completed_outreach(tmp_path, no_sleep):
    store, state = setup(tmp_path, [A], ["M1", "M2"])
    client = FakeGmailClient()
    inbox = FakeInbox()
    inbox.add_reply(A)
    run_campaign(make(client, store, state, inbox), Collector())
    assert state.is_finished()

    # Append a new recipient; outreach is no longer complete.
    state.add_emails([B])
    assert not state.outreach_complete()
    store.reset_cooldowns()
    inbox2 = FakeInbox()
    inbox2.add_reply(B)
    client2 = FakeGmailClient()
    run_campaign(make(client2, store, state, inbox2), Collector())

    assert [r.to for r in client2.firsts()] == [B]


# ------------------------------------------------------ phase 2: follow-up


def test_reply_triggers_a_threaded_second_message(tmp_path, no_sleep):
    store, state = setup(tmp_path, [A], ["Hello"], second="Thanks for replying!")
    client = FakeGmailClient()
    inbox = FakeInbox()
    inbox.add_reply(A, reply_message_id="<a-reply@mail>", references="<x@x>", subject="Re: Hello")
    run_campaign(make(client, store, state, inbox), Collector())

    reply = client.replies()[0]
    assert reply.to == A
    assert reply.body == "Thanks for replying!"
    assert reply.in_reply_to == "<a-reply@mail>"
    assert "<a-reply@mail>" in reply.references
    assert reply.subject == "Re: Hello"


def test_only_repliers_get_the_second_message(tmp_path, no_sleep):
    store, state = setup(tmp_path, [A, B], ["M1", "M2"])
    client = FakeGmailClient()
    inbox = FakeInbox()
    inbox.add_reply(A)  # B never replies
    campaign = make(client, store, state, inbox, poll=0.02)

    def maybe_stop(result):
        if result.email == A:
            campaign.stop()

    collector = Collector()
    cb = collector.callbacks()
    cb.on_second_result = maybe_stop
    campaign.start(cb)
    assert collector.done.wait(timeout=10)
    campaign._thread.join(timeout=5)

    assert [r.to for r in client.replies()] == [A]


def test_failed_reply_keeps_recipient_awaiting(tmp_path, no_sleep):
    store, state = setup(tmp_path, [A], ["M1"])
    client = FakeGmailClient(fail_reply=[A])
    inbox = FakeInbox()
    inbox.add_reply(A)
    campaign = make(client, store, state, inbox, poll=0.02)

    attempts = {"n": 0}

    def on_second(result):
        attempts["n"] += 1
        if attempts["n"] >= 3:
            campaign.stop()

    collector = Collector()
    cb = collector.callbacks()
    cb.on_second_result = on_second
    campaign.start(cb)
    assert collector.done.wait(timeout=10)
    campaign._thread.join(timeout=5)

    assert attempts["n"] >= 3
    assert state.get(A).status == "Sent"  # never marked Done


# --------------------------------------------------- concurrent follow-up


def test_reply_is_answered_during_outreach_not_after(tmp_path):
    """A reply to an early recipient goes out in the gap before the next send.

    Real (unpatched) timing: with a 1s interval and fast polling, A's reply must
    be sent before B's first message — proving the watcher runs concurrently.
    """
    store, state = setup(tmp_path, [A, B], ["M1", "M2"])
    client = FakeGmailClient()
    inbox = FakeInbox()
    inbox.add_reply(A)
    inbox.add_reply(B)
    campaign = make(client, store, state, inbox, interval=1, poll=0.05)

    run_campaign(campaign, Collector(), timeout=15)

    sequence = [(r.kind, r.to) for r in client.records]
    assert sequence.index(("reply", A)) < sequence.index(("first", B)), sequence
    assert {r.to for r in client.replies()} == {A, B}


# ------------------------------------------------------------ stop / state


def test_natural_completion_clears_active_flag(tmp_path, no_sleep):
    store, state = setup(tmp_path, [A], ["M1"])
    client = FakeGmailClient()
    inbox = FakeInbox()
    inbox.add_reply(A)
    collector = Collector()
    run_campaign(make(client, store, state, inbox), collector)

    assert collector.stopped == [False]
    assert state.active is False


def test_cannot_start_two_campaigns_at_once(tmp_path):
    store, state = setup(tmp_path, [A], ["M1"])
    client = FakeGmailClient()
    campaign = make(client, store, state, FakeInbox(), poll=0.05)

    assert campaign.start(Collector().callbacks())
    assert not campaign.start(Collector().callbacks())
    campaign.stop()
    campaign._thread.join(timeout=5)


# --------------------------------------------------------------- validation


def test_start_refused_when_not_logged_in(tmp_path):
    store, state = setup(tmp_path, [A], ["M1"])
    client = FakeGmailClient()
    client.is_logged_in = lambda: False
    campaign = make(client, store, state, FakeInbox())

    assert not campaign.start(Collector().callbacks())
    assert client.records == []


@pytest.mark.parametrize(
    "emails, subjects, second, fragment",
    [
        ([], ["M1"], "reply", "Recipient list is empty"),
        ([A], [], "reply", "at least one first message"),
        ([A], ["M1"], "", "second (reply) message is empty"),
    ],
)
def test_validation_messages(tmp_path, emails, subjects, second, fragment):
    store, state = setup(tmp_path, emails, subjects, second=(second or None), fresh=bool(emails))
    if second == "":
        store.set_second("")
    errors = validate_campaign(state.emails(), store)
    assert any(fragment in e for e in errors), errors


def test_deduplicate_emails_is_case_insensitive():
    unique, removed = deduplicate_emails([A, "Alice@Example.com", B, "  "])
    assert unique == [A, B]
    assert removed == 1
