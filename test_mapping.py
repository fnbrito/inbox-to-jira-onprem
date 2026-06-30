#!/usr/bin/env python3
"""Unit tests for the pure mapping/guard logic in email_to_jira.py.

These cover the parts that run with no network access:
    html_to_text, conversation_label, should_skip_message, build_issue_fields

Run:  python3 -m unittest -v test_mapping.py
"""
import types
import unittest

import email_to_jira as e2j


def fake_cfg():
    c = types.SimpleNamespace()
    c.project_key = "BISSB"
    c.issue_type = "Support Request"
    c.label_prefix = "eml"
    return c


class TestHtmlToText(unittest.TestCase):
    def test_plain_passthrough(self):
        self.assertEqual(e2j.html_to_text("hello world", False), "hello world")

    def test_strips_tags_and_unescapes(self):
        out = e2j.html_to_text("<p>Hi&amp;bye</p><div>line2</div>", True)
        self.assertIn("Hi&bye", out)
        self.assertIn("line2", out)
        self.assertNotIn("<p>", out)

    def test_drops_script_style(self):
        out = e2j.html_to_text("<style>x{}</style><p>keep</p><script>bad()</script>", True)
        self.assertIn("keep", out)
        self.assertNotIn("bad()", out)
        self.assertNotIn("x{}", out)

    def test_none_is_empty(self):
        self.assertEqual(e2j.html_to_text(None, True), "")


class TestConversationLabel(unittest.TestCase):
    def test_deterministic_and_safe(self):
        a = e2j.conversation_label("eml", "AAQkAD-conversation-id==")
        b = e2j.conversation_label("eml", "AAQkAD-conversation-id==")
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("eml-"))
        self.assertNotIn(" ", a)          # JQL-safe: no spaces
        self.assertNotIn("=", a)          # base64 padding stripped out by hashing

    def test_different_threads_differ(self):
        self.assertNotEqual(
            e2j.conversation_label("eml", "thread-1"),
            e2j.conversation_label("eml", "thread-2"),
        )


class TestShouldSkip(unittest.TestCase):
    def _msg(self, subject="Need help", sender="customer@example.com"):
        return {"subject": subject, "from": {"emailAddress": {"address": sender}}}

    def test_normal_message_not_skipped(self):
        skip, _ = e2j.should_skip_message(self._msg(), True)
        self.assertFalse(skip)

    def test_auto_reply_skipped(self):
        skip, reason = e2j.should_skip_message(self._msg(subject="Automatic reply: away"), True)
        self.assertTrue(skip)

    def test_noreply_sender_skipped(self):
        skip, _ = e2j.should_skip_message(self._msg(sender="no-reply@vendor.com"), True)
        self.assertTrue(skip)

    def test_loop_guard_on_own_address(self):
        skip, reason = e2j.should_skip_message(
            self._msg(sender="svc-bishelp@maplesoft.com"), True,
            own_addresses=("svc-bishelp@maplesoft.com",))
        self.assertTrue(skip)
        self.assertIn("loop", reason)

    def test_skip_disabled_lets_autoreply_through(self):
        skip, _ = e2j.should_skip_message(self._msg(subject="Automatic reply"), False)
        self.assertFalse(skip)


class TestBuildIssueFields(unittest.TestCase):
    def _msg(self):
        return {
            "subject": "Printer down on 3rd floor",
            "from": {"emailAddress": {"name": "Jane Doe", "address": "jane@acme.com"}},
            "receivedDateTime": "2026-06-26T10:00:00Z",
            "internetMessageId": "<abc@acme.com>",
            "conversationId": "conv-xyz",
        }

    def test_maps_core_fields(self):
        f = e2j.build_issue_fields(fake_cfg(), self._msg(), "The printer won't turn on.")
        self.assertEqual(f["project"], {"key": "BISSB"})
        self.assertEqual(f["issuetype"], {"name": "Support Request"})
        self.assertEqual(f["summary"], "Printer down on 3rd floor")
        self.assertIn("jane@acme.com", f["description"])
        self.assertIn("The printer won't turn on.", f["description"])
        self.assertEqual(f["labels"], [e2j.conversation_label("eml", "conv-xyz")])

    def test_summary_truncated_to_jira_limit(self):
        msg = self._msg()
        msg["subject"] = "x" * 400
        f = e2j.build_issue_fields(fake_cfg(), msg, "body")
        self.assertLessEqual(len(f["summary"]), 255)
        self.assertTrue(f["summary"].endswith("..."))

    def test_missing_subject_and_body(self):
        msg = self._msg()
        msg["subject"] = None
        f = e2j.build_issue_fields(fake_cfg(), msg, "")
        self.assertEqual(f["summary"], "(no subject)")
        self.assertIn("(no body)", f["description"])


class TestRequiredFields(unittest.TestCase):
    def _msg(self):
        return {"subject": "x", "from": {"emailAddress": {}}, "conversationId": "c"}

    def test_reporter_and_extra_fields_applied(self):
        c = fake_cfg()
        c.default_reporter = "svc-bishelp"
        c.extra_fields = {"customfield_10801": "3"}  # Tempo account id as a string
        f = e2j.build_issue_fields(c, self._msg(), "body")
        self.assertEqual(f["reporter"], {"name": "svc-bishelp"})
        self.assertEqual(f["customfield_10801"], "3")

    def test_no_reporter_when_unset(self):
        f = e2j.build_issue_fields(fake_cfg(), self._msg(), "body")
        self.assertNotIn("reporter", f)


class TestRecipientsAndReporter(unittest.TestCase):
    def _msg(self):
        return {
            "subject": "Help",
            "from": {"emailAddress": {"name": "Kari", "address": "kari@x.com"}},
            "toRecipients": [{"emailAddress": {"name": "BIS", "address": "bishelp@maplesoft.com"}}],
            "ccRecipients": [{"emailAddress": {"name": "Fran", "address": "fran@x.com"}}],
            "conversationId": "c",
        }

    def test_email_addresses(self):
        self.assertEqual(
            e2j.email_addresses([{"emailAddress": {"address": "A@B.com"}}, {"emailAddress": {}}]),
            ["A@B.com"])

    def test_format_recipients(self):
        self.assertEqual(e2j.format_recipients(self._msg()["ccRecipients"]), "Fran <fran@x.com>")
        self.assertEqual(e2j.format_recipients(None), "")

    def test_reporter_override_and_recipients_in_description(self):
        c = fake_cfg()
        c.default_reporter = "svc-bishelp"
        f = e2j.build_issue_fields(c, self._msg(), "body", reporter_override="kari")
        self.assertEqual(f["reporter"], {"name": "kari"})       # sender beats default
        self.assertIn("fran@x.com", f["description"])            # Cc captured
        self.assertIn("bishelp@maplesoft.com", f["description"]) # To captured

    def test_default_reporter_when_no_override(self):
        c = fake_cfg()
        c.default_reporter = "svc-bishelp"
        f = e2j.build_issue_fields(c, self._msg(), "body")
        self.assertEqual(f["reporter"], {"name": "svc-bishelp"})


class TestLinksAndInlineImages(unittest.TestCase):
    def test_link_preserved_as_wiki(self):
        out = e2j.html_to_text('<a href="https://x.com/f.pdf">f.pdf</a>', True)
        self.assertEqual(out, "[f.pdf|https://x.com/f.pdf]")

    def test_inline_image_embedded_when_mapped(self):
        out = e2j.html_to_text('<img src="cid:ABC123">', True, {"abc123": "logo.png"})
        self.assertIn("!logo.png!", out)

    def test_inline_image_skipped_when_unmapped(self):
        out = e2j.html_to_text('<img src="cid:zzz">hi', True, {})
        self.assertNotIn("!", out)
        self.assertIn("hi", out)


class TestSelectAttachments(unittest.TestCase):
    def _cfg(self):
        c = fake_cfg(); c.max_attachment_mb = 10; c.inline_image_min_kb = 20
        return c

    def test_filters_maps_and_references(self):
        atts = [
            {"kind": "file", "name": "logo.png", "ctype": "image/png", "size": 5 * 1024,
             "is_inline": True, "cid": "cidlogo", "data": b"x"},
            {"kind": "file", "name": "shot.png", "ctype": "image/png", "size": 50 * 1024,
             "is_inline": True, "cid": "cidshot", "data": b"x"},
            {"kind": "file", "name": "doc.zip", "ctype": "application/zip", "size": 1024,
             "is_inline": False, "cid": "", "data": b"x"},
            {"kind": "reference", "name": "Cloud.pdf", "url": "https://sp/Cloud.pdf"},
        ]
        up, inline, refs = e2j.select_attachments(self._cfg(), atts)
        names = [u[0] for u in up]
        self.assertIn("shot.png", names)
        self.assertIn("doc.zip", names)
        self.assertNotIn("logo.png", names)
        self.assertEqual(inline, {"cidshot": "shot.png"})
        self.assertEqual(refs, ["[Cloud.pdf|https://sp/Cloud.pdf]"])


class TestWikiFormatting(unittest.TestCase):
    def test_inline_styles(self):
        self.assertEqual(e2j.html_to_text("<b>Hi</b>", True), "*Hi*")
        self.assertEqual(e2j.html_to_text("<i>Hi</i>", True), "_Hi_")
        self.assertEqual(e2j.html_to_text("<u>Hi</u>", True), "+Hi+")
        self.assertEqual(e2j.html_to_text("<s>Hi</s>", True), "-Hi-")
        self.assertEqual(e2j.html_to_text("<code>x</code>", True), "{{x}}")

    def test_heading_and_lists(self):
        self.assertIn("h1. Title", e2j.html_to_text("<h1>Title</h1>", True))
        out = e2j.html_to_text("<ul><li>one</li><li>two</li></ul>", True)
        self.assertIn("* one", out); self.assertIn("* two", out)
        self.assertIn("# a", e2j.html_to_text("<ol><li>a</li></ol>", True))

    def test_colour_hex_rgb_and_skip_named(self):
        self.assertIn("{color:#0563c1}", e2j.html_to_text('<span style="color:#0563C1">b</span>', True))
        self.assertIn("{color:#ff0000}", e2j.html_to_text('<span style="color:rgb(255, 0, 0)">b</span>', True))
        self.assertNotIn("{color", e2j.html_to_text('<span style="color:windowtext">b</span>', True))

    def test_blockquote(self):
        self.assertIn("{quote}", e2j.html_to_text("<blockquote>q</blockquote>", True))


class TestHeaderToggleAndLabels(unittest.TestCase):
    def _msg(self):
        return {"subject": "Hi", "from": {"emailAddress": {"address": "a@b.com"}},
                "conversationId": "c"}

    def test_header_excluded(self):
        c = fake_cfg(); c.include_email_header = False
        f = e2j.build_issue_fields(c, self._msg(), "BODY")
        self.assertEqual(f["description"], "BODY")

    def test_header_included_by_default(self):
        f = e2j.build_issue_fields(fake_cfg(), self._msg(), "BODY")
        self.assertIn("*From:*", f["description"])
        self.assertIn("BODY", f["description"])

    def test_extra_labels_added_after_thread_key(self):
        c = fake_cfg(); c.extra_labels = ["email-intake", "bis"]
        f = e2j.build_issue_fields(c, self._msg(), "BODY")
        self.assertEqual(f["labels"][0], e2j.conversation_label("eml", "c"))
        self.assertIn("email-intake", f["labels"]); self.assertIn("bis", f["labels"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
