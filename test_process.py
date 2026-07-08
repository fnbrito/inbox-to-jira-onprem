#!/usr/bin/env python3
"""End-to-end test of process_once with all network calls mocked.

Verifies the orchestration: new mail -> create, auto-reply -> skip, reply on the
same thread -> comment (no duplicate), and that processed mail is marked read.
Run: python3 -m unittest -v test_process.py
"""
import types
import unittest

import email_to_jira as e2j


def cfg():
    return types.SimpleNamespace(
        project_key="SUP", issue_type="Support Request", label_prefix="eml",
        skip_auto_replies=True, mark_read=True, upload_attachments=True,
        jira_user="svc-inbox", mailbox="support@example.com",
        fetch_limit=25, max_attachment_mb=10.0,
    )


def msg(mid, subject, sender, conv, when):
    return {"id": mid, "subject": subject,
            "from": {"emailAddress": {"name": sender, "address": sender}},
            "conversationId": conv, "internetMessageId": f"<{mid}@x>",
            "receivedDateTime": when, "hasAttachments": False,
            "body": {"contentType": "text", "content": f"body of {mid}"}}


class ProcessOnceTest(unittest.TestCase):
    def setUp(self):
        self.created = {}      # label -> key
        self.comments = []     # (key, body)
        self.marked = []       # message ids marked read
        self.create_calls = []

        self.inbox = [
            msg("m1", "Printer broken", "alice@acme.com", "CONV-A", "2026-06-26T09:00:00Z"),
            msg("m2", "Automatic reply: OOO", "bob@acme.com", "CONV-B", "2026-06-26T09:01:00Z"),
            msg("m3", "RE: Printer broken", "alice@acme.com", "CONV-A", "2026-06-26T09:02:00Z"),
        ]

        e2j.graph_token = lambda c: "tok"
        e2j.jira_session = lambda c: object()
        e2j.fetch_unread = lambda c, t, top=None, only_unread=True: list(self.inbox)
        e2j.fetch_attachments = lambda c, t, mid: []
        e2j.mark_read = lambda c, t, mid: self.marked.append(mid)
        e2j.jira_find_issue_by_label = lambda c, s, label: self.created.get(label)

        def fake_create(c, s, fields):
            key = f"SUP-{len(self.created)+1}"
            self.created[fields["labels"][0]] = key
            self.create_calls.append(fields)
            return key
        e2j.jira_create_issue = fake_create
        e2j.jira_add_comment = lambda c, s, key, body: self.comments.append((key, body))

    def test_create_skip_and_thread(self):
        created, updated, skipped, errored = e2j.process_once(cfg(), dry_run=False)
        self.assertEqual((created, updated, skipped, errored), (1, 1, 1, 0))
        # exactly one issue created, as a Support Request
        self.assertEqual(len(self.create_calls), 1)
        self.assertEqual(self.create_calls[0]["issuetype"], {"name": "Support Request"})
        self.assertEqual(self.create_calls[0]["summary"], "Printer broken")
        # the reply commented on the SAME issue (no duplicate)
        self.assertEqual(len(self.comments), 1)
        self.assertEqual(self.comments[0][0], "SUP-1")
        # all three messages marked read (incl. the skipped auto-reply)
        self.assertEqual(sorted(self.marked), ["m1", "m2", "m3"])

    def test_dry_run_writes_nothing(self):
        created, updated, skipped, errored = e2j.process_once(cfg(), dry_run=True)
        # Dry-run persists nothing, so the reply on CONV-A can't find a parent
        # issue (none was created) and is previewed as a 2nd CREATE rather than a
        # comment. Live runs dedup correctly (see test_create_skip_and_thread).
        self.assertEqual((created, updated, skipped, errored), (2, 0, 1, 0))
        # The invariant that matters: dry-run must not write anything.
        self.assertEqual(self.create_calls, [])
        self.assertEqual(self.comments, [])
        self.assertEqual(self.marked, [])   # dry-run must not mark mail read


class TestProcessMoveMention(unittest.TestCase):
    def setUp(self):
        self.moved = []
        self.comments = []
        self.created = {}
        self.inbox = [
            {"id": "m1", "subject": "New issue",
             "from": {"emailAddress": {"name": "A", "address": "a@x.com"}},
             "conversationId": "C1", "internetMessageId": "<1>",
             "receivedDateTime": "2026-01-01T00:00:00Z", "hasAttachments": False,
             "body": {"contentType": "text", "content": "hi"}},
            {"id": "m2", "subject": "RE: New issue",
             "from": {"emailAddress": {"name": "A", "address": "a@x.com"}},
             "conversationId": "C1", "internetMessageId": "<2>",
             "receivedDateTime": "2026-01-01T00:01:00Z", "hasAttachments": False,
             "body": {"contentType": "text", "content": "reply"}},
        ]
        e2j.graph_token = lambda c: "tok"
        e2j.jira_session = lambda c: object()
        e2j.ensure_folder = lambda c, t, name: "FOLDER1"
        e2j.move_message = lambda c, t, mid, fid: self.moved.append(mid)
        e2j.fetch_unread = lambda c, t, top=None, only_unread=True: list(self.inbox)
        e2j.fetch_attachments = lambda c, t, mid: []
        e2j.mark_read = lambda c, t, mid: (_ for _ in ()).throw(
            AssertionError("mark_read must not be used when processed_folder is set"))
        e2j.jira_find_username = lambda c, s, email: "auser" if email == "a@x.com" else None
        e2j.jira_find_issue_by_label = lambda c, s, label: self.created.get(label)

        def create(c, s, fields):
            key = f"K-{len(self.created) + 1}"
            self.created[fields["labels"][0]] = key
            return key
        e2j.jira_create_issue = create
        e2j.jira_add_comment = lambda c, s, key, body: self.comments.append((key, body))
        e2j.apply_watchers = lambda c, s, key, msg, dry: None

    def _cfg(self):
        return types.SimpleNamespace(
            project_key="K", issue_type="Support Request", label_prefix="eml",
            skip_auto_replies=True, mark_read=True, upload_attachments=True,
            jira_user="svc", mailbox="box@x.com", fetch_limit=25, max_attachment_mb=10.0,
            reporter_from_sender=False, add_cc_watchers=False, inline_image_min_kb=20,
            include_email_header=True, extra_labels=[], extra_fields={}, label_rules={},
            processed_folder="Filed", mention_sender_in_comments=True)

    def test_move_and_mention(self):
        created, updated, skipped, errored = e2j.process_once(self._cfg(), dry_run=False)
        self.assertEqual((created, updated, skipped, errored), (1, 1, 0, 0))
        self.assertEqual(sorted(self.moved), ["m1", "m2"])       # moved, never mark_read
        self.assertEqual(len(self.comments), 1)
        self.assertTrue(self.comments[0][1].startswith("[~auser] "))  # sender @-mentioned


if __name__ == "__main__":
    unittest.main(verbosity=2)
