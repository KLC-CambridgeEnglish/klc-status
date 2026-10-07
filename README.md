# KLC system status

The public status page for KLC Cambridge English online systems: **https://status.klc.lk**

It lives on GitHub, deliberately **outside** the KLC production server, so it keeps working and can
tell people what is happening even when that server is down.

## What runs here

- `site/` holds the status page (English, Sinhala and Tamil), `site/status.json` (written by the
  monitor) and `site/notice.json` (written by people).
- `monitor.py` runs about every 5 minutes on GitHub Actions (`.github/workflows/monitor.yml`). It:
  - checks that each KLC service answers with its normal page;
  - checks that no HTTPS certificate is within 14 days of expiring;
  - rewrites `status.json` when anything changes, and at least once an hour.
- When something stops answering, the monitor opens an issue in this repository, assigned to the
  owner, so GitHub emails them. While the problem lasts it adds a reminder comment every hour. The
  issue closes itself after two healthy checks in a row.

Nothing private is stored here: no passwords, no keys, no student or business data.

**Limitation:** the checks prove that each site and app answers. They do not prove that every
feature or the database works end to end. A `/health` endpoint in each API will make the checks
stronger later.

## Posting a message on the page

To show a short message, for example about planned work, edit **`site/notice.json`** on GitHub. The
monitor never touches this file. Use all three languages if you can:

```json
{ "en": "Planned update tonight 9:00–9:15 pm.", "si": "", "ta": "" }
```

Empty the texts again to remove the message. Changes appear within a few minutes.

## Using status.json from other KLC pages

Fetch `https://status.klc.lk/status.json?t=<timestamp>` with `{ cache: "no-store" }`. GitHub Pages
allows cross-site reads but caches for up to 10 minutes, which is why the timestamp is added.

Stable fields:
- `state`: `operational`, `partial` or `outage`
- `since`: when the current state began
- `checked_at`: treat the status as unknown if this is more than 3 hours old
- `services[]`: `id`, `name` and `ok` for each service

`_monitor` is internal and may change.
