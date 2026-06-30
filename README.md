# email-to-jira-bridge

A small, dependency-light bridge that turns emails sent to a shared mailbox into
Jira issues. It reads a **Microsoft 365** mailbox via the **Microsoft Graph API**
using OAuth 2.0 (app-only) and creates issues through Jira's **REST API** — so it
needs no Jira upgrade and no paid marketplace app.

It was built for **on-premise Jira Server / Data Center** instances older than
8.22, which predate native OAuth 2.0 mail support.

## Why

Older Jira Server versions can only authenticate to a mailbox with **basic
authentication**, but Microsoft 365 has **permanently disabled basic auth** for
IMAP/POP. Jira only gained native OAuth 2.0 mail support in 8.22. This bridge does
the OAuth on the mail side and talks to Jira over REST, closing that gap on legacy
instances without an upgrade or a paid mail-handler app.

## Features

- Reads a mailbox over Microsoft Graph with OAuth 2.0 (app-only) — no basic auth.
- Creates issues of a configurable issue type; maps **subject → summary**, **body → description**.
- **Email thread → one issue:** replies are added as comments instead of duplicate
  issues (matched via a hashed conversation-id label).
- **Reporter from sender** (optional): resolves the email sender to a Jira user,
  falling back to a configurable default.
- **Watchers from Cc/To:** recipients who are Jira users are added as watchers;
  others are recorded in the description.
- **Attachments** carried across; **inline images embedded** in the description;
  tiny inline images (signature logos) filtered by size.
- **HTML → Jira wiki markup:** bold, italic, underline, strikethrough, headings,
  lists, blockquotes, text colour, hyperlinks.
- **Cloud/reference attachments** (e.g. OneDrive/SharePoint links) surfaced as links.
- Loop / auto-reply guards, plus `--preflight`, `--dry-run`, and targeted
  `--match-subject` testing.

## How it works

```
  M365 mailbox ──(Graph, OAuth2 app-only)──▶  email_to_jira.py  ──(Jira REST)──▶  Jira
                  read unread / mark read           │                create issue / comment
                  download attachments              ▼
                       de-dupes per email thread (hashed conversation id as a label):
                       first message of a thread -> CREATE; later replies -> COMMENT
```

## Requirements

- Python 3.8+ and `pip install -r requirements.txt` (only `requests`).
- A **Microsoft Entra** app registration with the Graph **Mail.ReadWrite**
  *application* permission (admin-consented) for the mailbox.
- A **Jira** account allowed to create issues/comments/attachments (and
  *Modify Reporter* / *Manage Watchers* if you enable those features).

## Installation

```bash
git clone https://github.com/<you>/<repo>.git
cd <repo>
pip install -r requirements.txt
cp config.example.ini config.ini      # then edit config.ini
```

## Configuration

All settings live in `config.ini` (see `config.example.ini` for the annotated
template). The two secrets can be supplied via environment variables
`GRAPH_CLIENT_SECRET` and `JIRA_PASSWORD` instead of the file.

| Section | Key | Purpose |
|---|---|---|
| `[graph]` | `tenant_id`, `client_id`, `client_secret`, `mailbox` | Entra app + target mailbox |
| `[jira]` | `base_url`, `user`, `password` | Jira instance + account |
| `[jira]` | `project_key`, `issue_type` | where/what to create |
| `[jira]` | `default_reporter` | reporter when none can be resolved |
| `[jira]` | `extra_fields` | JSON merged into create (required custom fields) |
| `[jira]` | `extra_labels` | static labels added to every issue |
| `[jira]` | `verify_ssl` | `true` / `false` / path to CA bundle |
| `[behavior]` | `mark_read`, `fetch_limit`, `skip_auto_replies` | polling behaviour |
| `[behavior]` | `reporter_from_sender`, `add_cc_watchers` | people mapping |
| `[behavior]` | `upload_attachments`, `max_attachment_mb`, `inline_image_min_kb` | attachments |
| `[behavior]` | `include_email_header` | include the From/To/Cc block in the description |

## Microsoft Entra (Azure AD) setup

1. **App registrations → New registration** (single tenant). Record the
   **Directory (tenant) ID** and **Application (client) ID**.
2. **Certificates & secrets → New client secret**; copy the value.
3. **API permissions → Microsoft Graph → Application permissions → `Mail.ReadWrite`**,
   then **Grant admin consent**.
4. Recommended: restrict the app to the single mailbox using an Exchange Online
   **Application Access Policy** or **RBAC for Applications**, so it can't read
   every mailbox in the tenant.

## Jira setup

Create a service account with **Browse Projects, Create Issues, Add Comments,
Create Attachments** in the target project (and **Modify Reporter** / **Manage
Watchers** if you enable `reporter_from_sender` / `add_cc_watchers`). Then verify:

```bash
python3 email_to_jira.py --preflight
```

Preflight checks connectivity and lists any **required create fields** with their
types and allowed values, so you know what to put in `extra_fields`.

## Usage

```bash
python3 email_to_jira.py --preflight                 # check config + connectivity
python3 email_to_jira.py --dry-run                   # show what WOULD happen; writes nothing
python3 email_to_jira.py                             # process the mailbox once
python3 email_to_jira.py --loop --interval 60        # keep polling every 60s
python3 email_to_jira.py --match-subject "TEXT"      # only process mail whose subject contains TEXT
python3 email_to_jira.py --describe-field <fieldId>  # dump a create-field's schema/allowed values
```

## Scheduling

**Linux (systemd timer)** — a `oneshot` service that runs one pass, driven by a
timer with `OnUnitActiveSec=1min`; or simply cron:

```cron
* * * * * cd /opt/email-to-jira && python3 email_to_jira.py >> bridge.log 2>&1
```

**Windows (Task Scheduler):**

```bat
schtasks /Create /TN "email-to-jira" /SC MINUTE /MO 1 ^
  /TR "python C:\email-to-jira\email_to_jira.py --config C:\email-to-jira\config.ini"
```

## Field mapping & formatting

- **Reporter** — the email sender if `reporter_from_sender` is on and they have a
  Jira account, else `default_reporter`. (For *forwarded* mail the sender is the
  forwarder, not the original requester.)
- **Watchers** — Cc/To who are Jira users; others are listed in the description.
- **Attachments** — uploaded when `upload_attachments` is on; inline images below
  `inline_image_min_kb` are dropped to skip signature logos.
- **Formatting** — HTML is converted to Jira wiki markup (best-effort). Your Jira
  Description field should use the **wiki renderer** for images/links to render.

## Testing

```bash
python3 -m unittest -v test_mapping.py test_process.py
```

The tests are offline (no network/credentials): they cover the HTML→wiki
conversion, attachment selection, thread-key labelling, field mapping, and the
create/skip/thread orchestration with all network calls mocked.

## Security

- **Never commit `config.ini` or secrets** (see `.gitignore`). Prefer env vars or a
  secrets manager; rotate the client secret before it expires.
- Scope the Entra app to a single mailbox; keep the Jira account least-privilege.
- Keep TLS verification on (`verify_ssl = true` or a CA bundle path).

## Limitations

- HTML→wiki conversion is best-effort; **tables are not converted** to Jira table
  markup (cells become line-separated text).
- Targets Jira Server / Data Center REST API v2 and a wiki-renderer description field.
- Reporter/watcher resolution only works for people who have Jira accounts.

## License

No license is included yet. Add one (e.g. MIT) before publishing if you want
others to reuse it.

## Disclaimer

Not affiliated with or endorsed by Atlassian or Microsoft. Provided as-is.
