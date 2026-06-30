#!/usr/bin/env python3
"""
email_to_jira.py - Path C-1 bridge for BIS Email-to-Jira automation.

Reads a Microsoft 365 mailbox (e.g. bishelp@maplesoft.com) via Microsoft Graph
using OAuth 2.0 (app-only / client-credentials), and creates Jira "Support
Request" issues on the on-prem Jira (8.6.1) via its REST API. Replies on the
same email thread are appended as a comment to the existing issue instead of
creating a duplicate.

Why this exists
---------------
Jira 8.6.1's native mail handler can only sign in to a mailbox with basic auth,
and Microsoft has permanently disabled basic auth for IMAP/POP on Microsoft 365.
Jira didn't gain OAuth 2.0 mail support until 8.22. This bridge performs the
OAuth on the M365 side (via Graph) and talks to Jira over its REST API, so it
needs no Jira upgrade and no paid marketplace app.

Modes
-----
    python3 email_to_jira.py --preflight   Check config + connectivity. Lists the
                                           Support Request issue-type id and any
                                           required create fields. Writes nothing.
    python3 email_to_jira.py --dry-run     Fetch mail and print what WOULD happen.
                                           Creates/comments/uploads nothing and
                                           does not mark mail as read.
    python3 email_to_jira.py               Process the mailbox once, for real.
    python3 email_to_jira.py --loop --interval 60
                                           Keep processing every N seconds.

Config: see config.example.ini. Copy it to config.ini and fill in. Secrets may be
supplied via env vars GRAPH_CLIENT_SECRET and JIRA_PASSWORD instead of the file.

Dependencies: requests  (pip install -r requirements.txt)
"""

import argparse
import base64
import configparser
import hashlib
import html
import json
import logging
import os
import re
import sys
import time
from html.parser import HTMLParser

import requests

GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com"
HTTP_TIMEOUT = 30
LOG = logging.getLogger("email_to_jira")


class BridgeError(Exception):
    """Recoverable bridge error (caught by preflight and the poll loop)."""


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
class Config:
    """Holds resolved settings from config.ini plus env-var secret overrides."""

    def __init__(self, path):
        cp = configparser.ConfigParser()
        if not cp.read(path):
            raise SystemExit(f"Config file not found or unreadable: {path}")

        # [graph]
        self.tenant_id = cp.get("graph", "tenant_id", fallback="").strip()
        self.client_id = cp.get("graph", "client_id", fallback="").strip()
        self.client_secret = (
            os.environ.get("GRAPH_CLIENT_SECRET")
            or cp.get("graph", "client_secret", fallback="")
        ).strip()
        self.mailbox = cp.get("graph", "mailbox", fallback="").strip()

        # [jira]
        self.base_url = cp.get("jira", "base_url", fallback="").strip().rstrip("/")
        self.jira_user = cp.get("jira", "user", fallback="").strip()
        self.jira_password = (
            os.environ.get("JIRA_PASSWORD")
            or cp.get("jira", "password", fallback="")
        ).strip()
        self.project_key = cp.get("jira", "project_key", fallback="").strip()
        self.issue_type = cp.get("jira", "issue_type", fallback="Support Request").strip()
        self.default_reporter = cp.get("jira", "default_reporter", fallback="").strip()
        self.extra_labels = [x.strip() for x in cp.get("jira", "extra_labels", fallback="").split(",") if x.strip()]
        raw_extra = cp.get("jira", "extra_fields", fallback="").strip()
        try:
            self.extra_fields = json.loads(raw_extra) if raw_extra else {}
        except json.JSONDecodeError as exc:
            raise SystemExit(f"[jira] extra_fields is not valid JSON: {exc}")
        verify = cp.get("jira", "verify_ssl", fallback="true").strip()
        # verify_ssl may be true/false or a path to a CA bundle
        if verify.lower() in ("true", "yes", "1"):
            self.verify_ssl = True
        elif verify.lower() in ("false", "no", "0"):
            self.verify_ssl = False
        else:
            self.verify_ssl = verify  # treat as CA bundle path

        # [behavior]
        self.mark_read = cp.getboolean("behavior", "mark_read", fallback=True)
        self.upload_attachments = cp.getboolean("behavior", "upload_attachments", fallback=True)
        self.max_attachment_mb = cp.getfloat("behavior", "max_attachment_mb", fallback=10.0)
        self.skip_auto_replies = cp.getboolean("behavior", "skip_auto_replies", fallback=True)
        self.fetch_limit = cp.getint("behavior", "fetch_limit", fallback=25)
        self.label_prefix = cp.get("behavior", "label_prefix", fallback="eml").strip()
        self.reporter_from_sender = cp.getboolean("behavior", "reporter_from_sender", fallback=False)
        self.add_cc_watchers = cp.getboolean("behavior", "add_cc_watchers", fallback=False)
        self.inline_image_min_kb = cp.getint("behavior", "inline_image_min_kb", fallback=20)
        self.include_email_header = cp.getboolean("behavior", "include_email_header", fallback=True)

    def validate(self):
        missing = []
        for attr, label in [
            ("tenant_id", "graph.tenant_id"),
            ("client_id", "graph.client_id"),
            ("client_secret", "graph.client_secret (or env GRAPH_CLIENT_SECRET)"),
            ("mailbox", "graph.mailbox"),
            ("base_url", "jira.base_url"),
            ("jira_user", "jira.user"),
            ("jira_password", "jira.password (or env JIRA_PASSWORD)"),
            ("project_key", "jira.project_key"),
        ]:
            if not getattr(self, attr):
                missing.append(label)
        if missing:
            raise SystemExit("Missing required config:\n  - " + "\n  - ".join(missing))


# --------------------------------------------------------------------------- #
# Small helpers (pure functions - covered by test_mapping.py)
# --------------------------------------------------------------------------- #
class _TextExtractor(HTMLParser):
    """Convert email HTML into Jira wiki markup (best-effort)."""

    _WRAP = {"b": "*", "strong": "*", "i": "_", "em": "_",
             "u": "+", "ins": "+", "s": "-", "strike": "-", "del": "-"}

    def __init__(self, inline_map=None):
        super().__init__()
        self._chunks = []
        self._skip = 0
        self._inline = inline_map or {}   # cid -> uploaded attachment filename
        self._lists = []                  # nesting of "*" (ul) / "#" (ol)
        self._spans = []                  # per span/font: the colour it opened (or None)
        self._a_href = None
        self._a_start = None
        self._h_start = None

    @staticmethod
    def _color(attrs):
        d = dict(attrs)
        m = re.search(r"(?:^|;)\s*color\s*:\s*([^;]+)", d.get("style", "") or "", re.I)
        c = (m.group(1) if m else d.get("color", "")).strip()
        if re.match(r"^#[0-9a-fA-F]{6}$", c):
            c = c.lower()
            return None if c == "#000000" else c   # black is the default; skip it
        m = re.match(r"rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)\s*$", c, re.I)
        if m:
            hexc = "#%02x%02x%02x" % tuple(int(x) for x in m.groups())
            return None if hexc == "#000000" else hexc
        return None  # skip named/unknown colours to avoid clutter

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "br" or tag in ("p", "div", "tr"):
            self._chunks.append("\n")
        elif tag in self._WRAP:
            self._chunks.append(self._WRAP[tag])
        elif tag in ("code", "tt"):
            self._chunks.append("{{")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._h_start = len(self._chunks)
        elif tag == "ul":
            self._lists.append("*")
        elif tag == "ol":
            self._lists.append("#")
        elif tag == "li":
            marker = (self._lists[-1] if self._lists else "*") * (len(self._lists) or 1)
            self._chunks.append(f"\n{marker} ")
        elif tag == "blockquote":
            self._chunks.append("\n{quote}\n")
        elif tag in ("span", "font"):
            color = self._color(attrs)
            self._spans.append(color)
            if color:
                self._chunks.append("{color:%s}" % color)
        elif tag == "a":
            self._a_href = dict(attrs).get("href", "")
            self._a_start = len(self._chunks)
        elif tag == "img":
            src = dict(attrs).get("src", "")
            if src.lower().startswith("cid:"):
                name = self._inline.get(src[4:].strip("<>").lower())
                if name:
                    self._chunks.append(f"\n!{name}!\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag in self._WRAP:
            self._chunks.append(self._WRAP[tag])
        elif tag in ("code", "tt"):
            self._chunks.append("}}")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            if self._h_start is not None:
                txt = re.sub(r"\s+", " ", "".join(self._chunks[self._h_start:])).strip()
                del self._chunks[self._h_start:]
                self._h_start = None
                if txt:
                    self._chunks.append(f"\n{tag}. {txt}\n")
        elif tag in ("ul", "ol"):
            if self._lists:
                self._lists.pop()
        elif tag == "blockquote":
            self._chunks.append("\n{quote}\n")
        elif tag in ("span", "font"):
            if self._spans and self._spans.pop():
                self._chunks.append("{color}")
        elif tag == "a" and self._a_start is not None:
            text = re.sub(r"\s+", " ", "".join(self._chunks[self._a_start:])).strip()
            del self._chunks[self._a_start:]
            href, self._a_href, self._a_start = self._a_href or "", None, None
            if href.lower().startswith(("http://", "https://")) and text:
                self._chunks.append(f"[{text.replace('|', ' ').replace(']', ')')}|{href}]")
            elif text:
                self._chunks.append(text)

    def handle_data(self, data):
        if not self._skip:
            self._chunks.append(data)


def html_to_text(content, is_html, inline_map=None):
    """Convert an email body to Jira wiki markup.

    Carries over bold/italic/underline/strike, headings, lists, blockquotes and
    text colour; hyperlinks become [text|url]; inline images whose cid is in
    inline_map become !filename!. Outlook HTML is messy, so this is best-effort.
    """
    if content is None:
        return ""
    if not is_html:
        return content.strip()
    parser = _TextExtractor(inline_map)
    parser.feed(content)
    text = html.unescape("".join(parser._chunks))
    lines = [ln.rstrip() for ln in text.splitlines()]
    out, blanks = [], 0
    for ln in lines:
        if ln.strip() == "":
            blanks += 1
            if blanks <= 1:
                out.append("")
        else:
            blanks = 0
            out.append(ln)
    return "\n".join(out).strip()


def conversation_label(prefix, conversation_id):
    """Deterministic, JQL-safe label derived from the email conversation id.

    Used to detect replies on the same thread so they become comments rather
    than duplicate issues.
    """
    digest = hashlib.sha1((conversation_id or "").encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def should_skip_message(msg, skip_auto_replies, own_addresses=()):
    """Return (skip: bool, reason: str). Guards against mail loops / noise."""
    subj = (msg.get("subject") or "").strip().lower()
    sender = (
        (msg.get("from") or {}).get("emailAddress", {}).get("address") or ""
    ).strip().lower()

    if sender and sender in {a.lower() for a in own_addresses}:
        return True, "sender is the bridge/Jira account (loop guard)"

    if skip_auto_replies:
        auto_subj = ("automatic reply", "auto:", "out of office", "undeliverable",
                     "delivery status notification")
        if subj.startswith(auto_subj):
            return True, f"auto-reply/bounce subject: {subj[:40]!r}"
        noise_senders = ("postmaster@", "mailer-daemon@")
        if sender.startswith(noise_senders) or "no-reply" in sender or "noreply" in sender:
            return True, f"system/no-reply sender: {sender!r}"
    return False, ""


def email_addresses(recipients):
    """Email addresses from a Graph recipients array."""
    out = []
    for r in recipients or []:
        addr = (r.get("emailAddress") or {}).get("address")
        if addr:
            out.append(addr)
    return out


def format_recipients(recipients):
    """Human-readable 'Name <addr>, ...' for a Graph recipients array."""
    parts = []
    for r in recipients or []:
        ea = r.get("emailAddress") or {}
        addr = ea.get("address", "")
        name = ea.get("name", "") or addr
        if addr:
            parts.append(f"{name} <{addr}>")
    return ", ".join(parts)


def build_issue_fields(cfg, msg, body_text, reporter_override=None):
    """Map a Graph message dict to Jira create-issue fields."""
    summary = (msg.get("subject") or "(no subject)").strip()
    if len(summary) > 254:  # Jira summary hard limit is 255
        summary = summary[:251] + "..."

    body_text = body_text or "(no body)"
    if getattr(cfg, "include_email_header", True):
        frm = (msg.get("from") or {}).get("emailAddress", {})
        sender_addr = frm.get("address", "(unknown)")
        sender_name = frm.get("name", "") or sender_addr
        to_line = format_recipients(msg.get("toRecipients"))
        cc_line = format_recipients(msg.get("ccRecipients"))
        head = ["Created automatically from email by the email_to_jira bridge.",
                "", f"*From:* {sender_name} <{sender_addr}>"]
        if to_line:
            head.append(f"*To:* {to_line}")
        if cc_line:
            head.append(f"*Cc:* {cc_line}")
        head.append(f"*Received:* {msg.get('receivedDateTime', '')}")
        head.append(f"*Message-Id:* {msg.get('internetMessageId', '')}")
        head += ["----", ""]
        description = "\n".join(head) + "\n" + body_text
    else:
        description = body_text

    labels = [conversation_label(cfg.label_prefix, msg.get("conversationId"))]
    labels += [x for x in getattr(cfg, "extra_labels", []) if x]

    fields = {
        "project": {"key": cfg.project_key},
        "issuetype": {"name": cfg.issue_type},
        "summary": summary,
        "description": description,
        "labels": labels,
    }
    # Reporter: prefer the resolved email sender (reporter_override), else default.
    reporter = reporter_override or getattr(cfg, "default_reporter", "")
    if reporter:
        fields["reporter"] = {"name": reporter}  # Jira Server matches users by name
    fields.update(getattr(cfg, "extra_fields", None) or {})
    return fields


# --------------------------------------------------------------------------- #
# Microsoft Graph
# --------------------------------------------------------------------------- #
def graph_token(cfg):
    url = f"{LOGIN}/{cfg.tenant_id}/oauth2/v2.0/token"
    data = {
        "client_id": cfg.client_id,
        "client_secret": cfg.client_secret,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }
    try:
        r = requests.post(url, data=data, timeout=HTTP_TIMEOUT)
    except requests.RequestException as exc:
        raise BridgeError(f"Could not reach Microsoft login endpoint: {exc}")
    if r.status_code != 200:
        raise BridgeError(f"Graph token request failed ({r.status_code}): {r.text}")
    return r.json()["access_token"]


def graph_get(token, path, params=None):
    r = requests.get(
        f"{GRAPH}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=HTTP_TIMEOUT,
    )
    if r.status_code >= 300:
        raise BridgeError(f"Graph {r.status_code} on {path}: {r.text[:600]}")
    return r.json()


def fetch_unread(cfg, token, top=None):
    """Return up to `top` (default fetch_limit) unread Inbox messages, oldest first."""
    data = graph_get(
        token,
        f"/users/{cfg.mailbox}/mailFolders/Inbox/messages",
        params={
            "$filter": "isRead eq false",
            "$top": str(top or cfg.fetch_limit),
            "$select": "id,subject,from,toRecipients,ccRecipients,receivedDateTime,"
                       "body,bodyPreview,hasAttachments,conversationId,internetMessageId",
        },
    )
    msgs = data.get("value", [])
    msgs.sort(key=lambda m: m.get("receivedDateTime", ""))  # oldest first
    return msgs


def fetch_attachments(cfg, token, message_id):
    """Return attachment metadata dicts (file + reference) for a message."""
    data = graph_get(token, f"/users/{cfg.mailbox}/messages/{message_id}/attachments")
    out = []
    for a in data.get("value", []):
        t = a.get("@odata.type", "")
        if t == "#microsoft.graph.fileAttachment":
            out.append({
                "kind": "file",
                "name": a.get("name", "attachment"),
                "ctype": a.get("contentType", "application/octet-stream"),
                "size": a.get("size", 0),
                "is_inline": bool(a.get("isInline")),
                "cid": (a.get("contentId") or "").strip("<>").lower(),
                "data": base64.b64decode(a.get("contentBytes", "")),
            })
        elif t == "#microsoft.graph.referenceAttachment":
            out.append({"kind": "reference", "name": a.get("name", "link"),
                        "url": a.get("sourceUrl", "")})
        # itemAttachment (attached emails) skipped in this prototype
    return out


def select_attachments(cfg, atts):
    """Return (to_upload, inline_map, reference_lines).

    to_upload: [(name, ctype, data)] to attach; inline_map: {cid: filename} for
    kept inline images; reference_lines: '[name|url]' for cloud/reference links.
    Inline images smaller than inline_image_min_kb are dropped (signature logos).
    """
    min_inline = int(getattr(cfg, "inline_image_min_kb", 20)) * 1024
    max_bytes = cfg.max_attachment_mb * 1024 * 1024
    to_upload, inline_map, refs = [], {}, []
    for a in atts:
        if a["kind"] == "reference":
            if a.get("url"):
                refs.append(f"[{a['name']}|{a['url']}]")
            continue
        if a["size"] > max_bytes:
            LOG.warning("  skipping oversized attachment %s (%s bytes)", a["name"], a["size"])
            continue
        if a["is_inline"] and a["size"] < min_inline:
            continue  # drop tiny inline images (e.g. signature logos)
        to_upload.append((a["name"], a["ctype"], a["data"]))
        if a["is_inline"] and a["cid"]:
            inline_map[a["cid"]] = a["name"]
    return to_upload, inline_map, refs


def mark_read(cfg, token, message_id):
    r = requests.patch(
        f"{GRAPH}/users/{cfg.mailbox}/messages/{message_id}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"isRead": True},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()


# --------------------------------------------------------------------------- #
# Jira
# --------------------------------------------------------------------------- #
def jira_session(cfg):
    s = requests.Session()
    s.auth = (cfg.jira_user, cfg.jira_password)
    s.verify = cfg.verify_ssl
    s.headers.update({"Content-Type": "application/json", "Accept": "application/json"})
    return s


def jira_find_issue_by_label(cfg, s, label):
    r = s.get(
        f"{cfg.base_url}/rest/api/2/search",
        params={"jql": f'project = "{cfg.project_key}" AND labels = "{label}"',
                "fields": "key", "maxResults": 1},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    issues = r.json().get("issues", [])
    return issues[0]["key"] if issues else None


def jira_create_issue(cfg, s, fields):
    r = s.post(f"{cfg.base_url}/rest/api/2/issue", json={"fields": fields}, timeout=HTTP_TIMEOUT)
    if r.status_code >= 300:
        raise RuntimeError(f"Jira create failed ({r.status_code}): {r.text}")
    return r.json()["key"]


def jira_add_comment(cfg, s, key, body):
    r = s.post(f"{cfg.base_url}/rest/api/2/issue/{key}/comment", json={"body": body},
               timeout=HTTP_TIMEOUT)
    if r.status_code >= 300:
        raise RuntimeError(f"Jira comment failed ({r.status_code}): {r.text}")


def jira_upload_attachments(cfg, s, key, attachments):
    if not attachments:
        return
    url = f"{cfg.base_url}/rest/api/2/issue/{key}/attachments"
    headers = {"X-Atlassian-Token": "no-check"}  # required for file upload
    for name, ctype, data in attachments:
        files = {"file": (name, data, ctype)}
        # don't send the JSON Content-Type header on a multipart request
        r = requests.post(url, headers=headers, files=files, auth=s.auth,
                          verify=cfg.verify_ssl, timeout=HTTP_TIMEOUT)
        if r.status_code >= 300:
            LOG.warning("  attachment upload failed for %s (%s): %s",
                        name, r.status_code, r.text[:200])


# --------------------------------------------------------------------------- #
# Core processing
# --------------------------------------------------------------------------- #
def jira_find_username(cfg, s, email):
    """Return the Jira username (Server 'name') for an email address, or None."""
    if not email:
        return None
    try:
        r = s.get(f"{cfg.base_url}/rest/api/2/user/search",
                  params={"username": email, "maxResults": 1}, timeout=HTTP_TIMEOUT)
    except requests.RequestException:
        return None
    if r.status_code >= 300:
        return None
    for u in (r.json() or []):
        if u.get("active", True):
            return u.get("name")
    return None


def jira_add_watcher(cfg, s, key, username):
    """Add a watcher by username (Jira Server). Returns True on success."""
    r = s.post(f"{cfg.base_url}/rest/api/2/issue/{key}/watchers",
               json=username, timeout=HTTP_TIMEOUT)
    return r.status_code < 300


def apply_watchers(cfg, s, key, msg, dry_run):
    """Add Cc/To recipients who are Jira users as watchers; log the rest."""
    if not getattr(cfg, "add_cc_watchers", False):
        return
    skip = {(cfg.mailbox or "").lower(), (cfg.jira_user or "").lower()}
    seen, added, missed = set(), [], []
    for addr in email_addresses(msg.get("ccRecipients")) + email_addresses(msg.get("toRecipients")):
        low = addr.lower()
        if low in skip or low in seen:
            continue
        seen.add(low)
        un = jira_find_username(cfg, s, addr)
        if not un:
            missed.append(addr)
        elif dry_run:
            added.append(un)
        elif jira_add_watcher(cfg, s, key, un):
            added.append(un)
    if added:
        LOG.info("  watchers %s: %s", "would add" if dry_run else "added", ", ".join(added))
    if missed:
        LOG.info("  cc/to with no Jira user (not watched): %s", ", ".join(missed))


def process_once(cfg, dry_run=False, match_subject=None):
    token = graph_token(cfg)
    s = jira_session(cfg)
    own = (cfg.jira_user, cfg.mailbox)

    msgs = fetch_unread(cfg, token, top=200 if match_subject else None)
    if match_subject:
        msgs = [m for m in msgs if match_subject.lower() in (m.get("subject") or "").lower()]
    LOG.info("Fetched %d message(s) to process from %s", len(msgs), cfg.mailbox)

    created = updated = skipped = errored = 0
    for msg in msgs:
        subject = (msg.get("subject") or "(no subject)").strip()
        try:
            skip, reason = should_skip_message(msg, cfg.skip_auto_replies, own)
            if skip:
                LOG.info("SKIP  %-55.55s | %s", subject, reason)
                if not dry_run and cfg.mark_read:
                    mark_read(cfg, token, msg["id"])
                skipped += 1
                continue

            atts = (fetch_attachments(cfg, token, msg["id"])
                    if msg.get("hasAttachments") else [])
            to_upload, inline_map, ref_lines = select_attachments(cfg, atts)
            body_text = html_to_text(
                (msg.get("body") or {}).get("content"),
                (msg.get("body") or {}).get("contentType", "text").lower() == "html",
                inline_map,
            )
            if ref_lines:
                body_text += "\n\n*Linked files:*\n" + "\n".join(f"* {r}" for r in ref_lines)
            label = conversation_label(cfg.label_prefix, msg.get("conversationId"))
            existing = jira_find_issue_by_label(cfg, s, label)

            if existing:
                if dry_run:
                    LOG.info("DRY   would COMMENT on %s <- reply %r", existing, subject)
                else:
                    frm = (msg.get("from") or {}).get("emailAddress", {})
                    jira_add_comment(
                        cfg, s, existing,
                        f"Reply received by email from {frm.get('name', '')} "
                        f"<{frm.get('address', '')}> on {msg.get('receivedDateTime', '')}:"
                        f"\n\n{body_text}",
                    )
                    if cfg.upload_attachments and to_upload:
                        jira_upload_attachments(cfg, s, existing, to_upload)
                    LOG.info("COMMENT %s <- %r", existing, subject)
                apply_watchers(cfg, s, existing, msg, dry_run)
                updated += 1
            else:
                reporter_override = None
                if getattr(cfg, "reporter_from_sender", False):
                    sender_addr = (msg.get("from") or {}).get("emailAddress", {}).get("address")
                    reporter_override = jira_find_username(cfg, s, sender_addr)
                    if reporter_override:
                        LOG.info("  reporter <- sender %s (Jira user %s)", sender_addr, reporter_override)
                    else:
                        LOG.info("  sender %s has no Jira user; using default_reporter", sender_addr)
                fields = build_issue_fields(cfg, msg, body_text, reporter_override)
                if dry_run:
                    LOG.info("DRY   would CREATE %s/%s : %r",
                             cfg.project_key, cfg.issue_type, fields["summary"])
                    apply_watchers(cfg, s, None, msg, dry_run)
                else:
                    key = jira_create_issue(cfg, s, fields)
                    if cfg.upload_attachments and to_upload:
                        jira_upload_attachments(cfg, s, key, to_upload)
                    LOG.info("CREATE %s <- %r", key, fields["summary"])
                    apply_watchers(cfg, s, key, msg, dry_run)
                created += 1

            if not dry_run and cfg.mark_read:
                mark_read(cfg, token, msg["id"])

        except Exception as exc:  # one bad message must not stop the batch
            errored += 1
            LOG.error("ERROR processing %r: %s", subject, exc)
            # leave the message unread so the next run retries it

    LOG.info("Done. created=%d updated=%d skipped=%d errored=%d%s",
             created, updated, skipped, errored,
             "  (DRY-RUN: nothing written)" if dry_run else "")
    return created, updated, skipped, errored


# --------------------------------------------------------------------------- #
# Preflight
# --------------------------------------------------------------------------- #
def list_accounts(cfg):
    "List Tempo accounts (id/key/name) for the required Account field, then exit."
    s = jira_session(cfg)
    for ep in ("/rest/tempo-accounts/1/account", "/rest/tempo-accounts/3/account"):
        try:
            r = s.get(f"{cfg.base_url}{ep}", timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:
            print(f"{ep}: {exc}")
            continue
        if r.status_code != 200:
            print(f"{ep}: HTTP {r.status_code}")
            continue
        try:
            data = r.json()
        except ValueError:
            print(f"{ep}: 200 but response was not JSON")
            continue
        accounts = data if isinstance(data, list) else (data.get("accounts") or data.get("results") or [])
        if not accounts:
            print(f"{ep}: 200 but no accounts found")
            return
        print(f"Accounts from {ep}:")
        for a in accounts:
            print(f"  id={a.get('id')}  key={a.get('key')}  name={a.get('name')}  status={a.get('status')}")
        print("Then set in config.ini, e.g.:  extra_fields = {\"customfield_10801\": {\"id\": <id>}}")
        return
    print("Could not list accounts - confirm Tempo is installed and check the REST path/version.")


def describe_field(cfg, field_id):
    """Print one create-field's createmeta JSON (schema + allowedValues), then exit."""
    s = jira_session(cfg)
    meta = s.get(
        f"{cfg.base_url}/rest/api/2/issue/createmeta",
        params={"projectKeys": cfg.project_key, "issuetypeNames": cfg.issue_type,
                "expand": "projects.issuetypes.fields"},
        timeout=HTTP_TIMEOUT,
    )
    meta.raise_for_status()
    its = (meta.json().get("projects") or [{}])[0].get("issuetypes") or [{}]
    fields = its[0].get("fields", {})
    f = fields.get(field_id)
    if not f:
        print(f"{field_id} not on the create screen. Available: " + ", ".join(sorted(fields)))
        return
    print(json.dumps(f, indent=2)[:4000])


def preflight(cfg):
    ok = True
    print("== Preflight ==")
    cfg.validate()
    print("config: required fields present ........ OK")

    # Graph: token + mailbox read (proves app registration, secret, consent, access policy)
    try:
        token = graph_token(cfg)
        print("graph: obtained access token ........... OK")
        data = graph_get(token, f"/users/{cfg.mailbox}/mailFolders/Inbox",
                         params={"$select": "totalItemCount,unreadItemCount"})
        print(f"graph: read mailbox {cfg.mailbox} ...... OK "
              f"(total={data.get('totalItemCount')}, unread={data.get('unreadItemCount')})")
    except Exception as exc:
        ok = False
        print(f"graph: FAILED -> {exc}")

    # Jira: auth + project + createmeta for the issue type
    try:
        s = jira_session(cfg)
        me = s.get(f"{cfg.base_url}/rest/api/2/myself", timeout=HTTP_TIMEOUT)
        if me.status_code >= 300:
            reason = me.headers.get("X-Authentication-Denied-Reason", "")
            raise BridgeError(f"{me.status_code} on /myself: {me.text[:300]}"
                              + (f"  [{reason}]" if reason else ""))
        print(f"jira: authenticated as {me.json().get('name')} .. OK")

        meta = s.get(
            f"{cfg.base_url}/rest/api/2/issue/createmeta",
            params={"projectKeys": cfg.project_key, "issuetypeNames": cfg.issue_type,
                    "expand": "projects.issuetypes.fields"},
            timeout=HTTP_TIMEOUT,
        )
        meta.raise_for_status()
        projects = meta.json().get("projects", [])
        if not projects:
            ok = False
            print(f"jira: project {cfg.project_key!r} not visible to this user -> FAILED")
        else:
            itypes = projects[0].get("issuetypes", [])
            match = next((it for it in itypes if it["name"].lower() == cfg.issue_type.lower()), None)
            if not match:
                ok = False
                avail = ", ".join(it["name"] for it in itypes)
                print(f"jira: issue type {cfg.issue_type!r} not on project. Available: {avail} -> FAILED")
            else:
                print(f"jira: issue type {cfg.issue_type!r} id={match['id']} ... OK")
                req = [(k, v) for k, v in match.get("fields", {}).items()
                       if v.get("required") and k not in ("project", "issuetype", "summary")]
                if req:
                    print("jira: ADDITIONAL required fields on create -> set via "
                          "[jira] default_reporter / extra_fields:")
                    for k, v in req:
                        schema = v.get("schema", {})
                        t = schema.get("type", "?")
                        if schema.get("items"):
                            t += f"<{schema.get('items')}>"
                        line = f"        - {k} ({v.get('name')}) type={t}"
                        allowed = v.get("allowedValues") or []
                        if allowed:
                            vals = [str(o.get("value") or o.get("name") or o.get("id")) for o in allowed[:12]]
                            line += " allowed=[" + ", ".join(vals) + ("" if len(allowed) <= 12 else ", ...") + "]"
                        print(line)
                else:
                    print("jira: no extra required create fields ... OK")
    except Exception as exc:
        ok = False
        print(f"jira: FAILED -> {exc}")

    print("== Preflight", "PASSED ==" if ok else "FAILED ==")
    return ok


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(description="Bridge M365 mailbox -> Jira Support Request issues.")
    ap.add_argument("-c", "--config", default="config.ini", help="Path to config.ini (default: ./config.ini)")
    ap.add_argument("--preflight", action="store_true", help="Validate config + connectivity, then exit")
    ap.add_argument("--describe-field", metavar="ID",
                    help="Dump one create-field's schema + allowed values, then exit")
    ap.add_argument("--list-accounts", action="store_true",
                    help="List Tempo accounts for the Account field, then exit")
    ap.add_argument("--dry-run", action="store_true", help="Fetch mail and print intended actions only")
    ap.add_argument("--match-subject", metavar="TEXT",
                    help="Only process unread mail whose subject contains TEXT (targeted testing)")
    ap.add_argument("--loop", action="store_true", help="Keep polling instead of a single pass")
    ap.add_argument("--interval", type=int, default=60, help="Seconds between polls when --loop (default 60)")
    ap.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    cfg = Config(args.config)

    if args.describe_field:
        describe_field(cfg, args.describe_field)
        sys.exit(0)

    if args.list_accounts:
        list_accounts(cfg)
        sys.exit(0)

    if args.preflight:
        sys.exit(0 if preflight(cfg) else 1)

    cfg.validate()

    if args.loop:
        LOG.info("Starting poll loop every %ds (Ctrl-C to stop)", args.interval)
        while True:
            try:
                process_once(cfg, dry_run=args.dry_run, match_subject=args.match_subject)
            except Exception as exc:
                LOG.error("Pass failed: %s", exc)
            time.sleep(args.interval)
    else:
        try:
            process_once(cfg, dry_run=args.dry_run, match_subject=args.match_subject)
        except BridgeError as exc:
            LOG.error("%s", exc)
            sys.exit(1)


if __name__ == "__main__":
    main()
