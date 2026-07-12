"""Tests for filling the first-message pool in bulk: pasted text, CSV, and folders."""

import pytest

from message_store import (
    MessageStore,
    parse_bulk_text,
    parse_csv_file,
    parse_folder,
)


@pytest.fixture
def store(tmp_path):
    return MessageStore(tmp_path / "messages.json")


# -------------------------------------------------------------- pasted text


def test_pasted_messages_split_on_the_dashed_separator():
    drafts = parse_bulk_text(
        "Subject one\nBody one\n"
        "---\n"
        "Subject two\nBody two\n"
    )
    assert drafts == [("Subject one", "Body one"), ("Subject two", "Body two")]


def test_a_blank_line_does_not_split_a_multi_paragraph_body():
    """The whole point of the `---` rule: real bodies have paragraphs in them."""
    drafts = parse_bulk_text("Subject\n\nHi there,\n\nSecond paragraph.\n\nBest,\nMe")
    assert len(drafts) == 1
    subject, body = drafts[0]
    assert subject == "Subject"
    assert body == "Hi there,\n\nSecond paragraph.\n\nBest,\nMe"


def test_a_trailing_separator_does_not_add_an_empty_message():
    assert parse_bulk_text("Subject\nBody\n---\n") == [("Subject", "Body")]


def test_a_block_with_no_body_is_skipped():
    assert parse_bulk_text("Just a subject\n---\nSubject\nBody") == [("Subject", "Body")]


def test_empty_text_yields_nothing():
    assert parse_bulk_text("   \n\n") == []


def test_a_separator_needs_its_own_line():
    """A dash inside prose (an em-dash run, a signature rule) must not split anything."""
    drafts = parse_bulk_text("Subject\nBody --- with dashes inline")
    assert len(drafts) == 1


# ---------------------------------------------------------------------- CSV


def test_csv_with_a_header_reads_by_column_name(tmp_path):
    path = tmp_path / "messages.csv"
    path.write_text("body,subject\nBody one,Subject one\nBody two,Subject two\n", encoding="utf-8")
    assert parse_csv_file(path) == [("Subject one", "Body one"), ("Subject two", "Body two")]


def test_csv_without_a_header_falls_back_to_the_first_two_columns(tmp_path):
    path = tmp_path / "messages.csv"
    path.write_text("Subject one,Body one\nSubject two,Body two\n", encoding="utf-8")
    assert parse_csv_file(path) == [("Subject one", "Body one"), ("Subject two", "Body two")]


def test_csv_rows_missing_a_subject_or_body_are_skipped(tmp_path):
    path = tmp_path / "messages.csv"
    path.write_text("subject,body\nSubject one,\n,Body two\nGood,Body\n", encoding="utf-8")
    assert parse_csv_file(path) == [("Good", "Body")]


def test_csv_survives_the_bom_excel_writes(tmp_path):
    path = tmp_path / "messages.csv"
    path.write_text("subject,body\nSubject,Body\n", encoding="utf-8-sig")
    assert parse_csv_file(path) == [("Subject", "Body")]


def test_an_empty_csv_yields_nothing(tmp_path):
    path = tmp_path / "messages.csv"
    path.write_text("", encoding="utf-8")
    assert parse_csv_file(path) == []


# ------------------------------------------------------------------- folder


def test_each_text_file_in_a_folder_becomes_one_message(tmp_path):
    (tmp_path / "a.txt").write_text("Subject A\nBody A", encoding="utf-8")
    (tmp_path / "b.md").write_text("Subject B\n\nBody B", encoding="utf-8")
    (tmp_path / "notes.pdf").write_bytes(b"%PDF-1.4 not a message")

    assert parse_folder(tmp_path) == [("Subject A", "Body A"), ("Subject B", "Body B")]


def test_a_folder_of_nothing_useful_yields_nothing(tmp_path):
    (tmp_path / "empty.txt").write_text("", encoding="utf-8")
    assert parse_folder(tmp_path) == []


# ------------------------------------------------------------------- store


def test_importing_many_messages_saves_once_and_persists(store, tmp_path):
    added = store.add_many_first([("S1", "B1"), ("S2", "B2"), ("S3", "B3")])
    assert added == 3

    reloaded = MessageStore(tmp_path / "messages.json")
    assert [m.subject for m in reloaded.first_pool] == ["S1", "S2", "S3"]


def test_importing_skips_blank_drafts(store):
    assert store.add_many_first([("S1", "B1"), ("", "B2"), ("S3", "  ")]) == 1
    assert len(store.first_pool) == 1


def test_a_duplicate_lands_next_to_its_original_and_is_ready_to_send(store):
    first = store.add_first("Original", "Body")
    store.add_first("Other", "Body")
    first.mark_sent()

    copy = store.duplicate_first(first.id)

    assert copy.id != first.id
    assert (copy.subject, copy.body) == ("Original", "Body")
    # The original keeps its 24h lock; the copy is a new message and free to send.
    assert copy.is_available()
    assert not first.is_available()
    assert [m.subject for m in store.first_pool] == ["Original", "Original", "Other"]


def test_duplicating_something_that_is_gone_returns_nothing(store):
    assert store.duplicate_first("no-such-id") is None


def test_deleting_a_selection_removes_exactly_that_selection(store):
    a = store.add_first("A", "Body")
    b = store.add_first("B", "Body")
    c = store.add_first("C", "Body")

    assert store.delete_many_first([a.id, c.id]) == 2
    assert [m.subject for m in store.first_pool] == ["B"]
    assert store.get_first(b.id) is not None


def test_deleting_nothing_changes_nothing(store):
    store.add_first("A", "Body")
    assert store.delete_many_first([]) == 0
    assert len(store.first_pool) == 1
