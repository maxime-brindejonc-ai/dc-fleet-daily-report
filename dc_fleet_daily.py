#!/usr/bin/env python3
"""DC Fleet daily status — pulls Fleetio data and updates a Confluence page.

Required environment:
  FLEETIO_API_TOKEN, FLEETIO_ACCOUNT_TOKEN
  ATLASSIAN_EMAIL, ATLASSIAN_API_TOKEN
  ATLASSIAN_DOMAIN  (e.g. appliedintuition.atlassian.net)
  CONFLUENCE_PAGE_ID
  JIRA_PROJECTS  (comma-separated, e.g. VSTAB,VBUILD,VCO,AVP)

Optional:
  STATE_PATH  (default: state.json)
  VEHICLE_IDS_PATH  (default: dc_vehicle_ids.json)
  SKIP_IF_ONLY_OPERATORS  (default: "1"; set to "0" to publish on every run)
  DRY_RUN  (default: "0"; set to "1" to print body instead of publishing)
"""
import base64
import html
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

FLEETIO_BASE = "https://secure.fleetio.com/api/v1"
SCRIPT_TZ = ZoneInfo("America/Los_Angeles")
STATUS_EMOJI = {
    "Active": "🟢",
    "Verification": "🟡",
    "Validation": "🟠",
    "Calibration": "🩷",
    "Build": "🟣",
    "Out of Service": "🔴",
    "Inactive": "🔘",
}


def env(name, default=None, required=True):
    v = os.environ.get(name, default)
    if required and not v:
        sys.exit(f"missing env: {name}")
    return v


def fleetio_get(path, params=None):
    headers = {
        "Authorization": f"Token {env('FLEETIO_API_TOKEN')}",
        "Account-Token": env("FLEETIO_ACCOUNT_TOKEN"),
    }
    r = requests.get(f"{FLEETIO_BASE}{path}", headers=headers, params=params or {}, timeout=30)
    if r.status_code == 429:
        return {"_rate_limited": True}
    r.raise_for_status()
    return r.json()


def fleetio_paginate(path, max_pages=80):
    pages = []
    cursor = None
    for i in range(max_pages):
        params = {"per_page": 100}
        if cursor:
            params["start_cursor"] = cursor
        page = fleetio_get(path, params)
        if page.get("_rate_limited"):
            print(f"  rate-limited at page {i + 1}; using partial data", file=sys.stderr)
            return pages, True
        pages.append(page)
        cursor = page.get("next_cursor")
        if not cursor:
            return pages, False
    return pages, False


def load_vehicle_ids():
    path = env("VEHICLE_IDS_PATH", "dc_vehicle_ids.json", required=False)
    with open(path) as f:
        return set(json.load(f))


def pull_fleetio_state():
    dc_ids = load_vehicle_ids()
    veh_pages, _ = fleetio_paginate("/vehicles", max_pages=10)
    vehicles = {}
    for page in veh_pages:
        for r in page.get("records", []):
            if r["id"] not in dc_ids:
                continue
            cf = r.get("custom_fields") or {}
            drv = r.get("driver") or {}
            vehicles[r["name"]] = {
                "id": r["id"],
                "status": r["vehicle_status_name"],
                "activity": cf.get("activity") or "",
                "location": cf.get("current_location_new_") or "",
                "notes": cf.get("triage_notes") or "",
                "driver_name": drv.get("name"),
            }

    asg_pages, rate_limited = fleetio_paginate("/vehicle_assignments", max_pages=80)
    asg_op = {}
    for page in asg_pages:
        for r in page.get("records", []):
            if not r.get("current"):
                continue
            v = r.get("vehicle") or {}
            if v.get("id") in dc_ids and v.get("name") not in asg_op:
                asg_op[v["name"]] = (r.get("contact") or {}).get("name")

    disagreements = []
    for name, v in vehicles.items():
        drv = v["driver_name"]
        asg = asg_op.get(name)
        if drv != asg and (drv or asg):
            disagreements.append((name, drv, asg))
        v["operator"] = drv or asg or None

    return vehicles, disagreements, rate_limited


def load_prev_state():
    path = env("STATE_PATH", "state.json", required=False)
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f).get("vehicles", {})


def save_state(vehicles):
    path = env("STATE_PATH", "state.json", required=False)
    sanitized = {
        name: {"status": v["status"], "notes": v["notes"]}
        for name, v in vehicles.items()
    }
    with open(path, "w") as f:
        json.dump(
            {
                "last_run_utc": datetime.utcnow().isoformat() + "Z",
                "vehicles": sanitized,
            },
            f,
            indent=2,
            sort_keys=True,
        )


def build_changes(prev, curr):
    out = {"status": [], "notes": []}
    for name in sorted(set(curr) & set(prev), key=lambda n: int(n.split("-")[1])):
        p, c = prev[name], curr[name]
        if p["status"] != c["status"]:
            out["status"].append((name, p["status"], c["status"]))
        if (p.get("notes") or "").strip() != (c["notes"] or "").strip():
            out["notes"].append((name, p.get("notes") or "", c["notes"]))
    return out


def cell(text):
    """Render a cell value wrapped in <p>: HTML-escape, linkify, then paragraph-wrap.

    Wrapping in <p> prevents Confluence's storage parser from splitting on literal `|`
    characters as if they were wiki-format table delimiters.
    """
    if not text:
        return "<p></p>"
    escaped = html.escape(text, quote=False)
    return f"<p>{linkify(escaped)}</p>"


def linkify(escaped_text):
    """Wrap PROJ-NNN refs in <a> tags. Input must already be HTML-escaped."""
    domain = env("ATLASSIAN_DOMAIN")
    projects = env("JIRA_PROJECTS", "VSTAB,VBUILD,VCO,AVP", required=False).split(",")
    pattern = re.compile(rf"\b({'|'.join(projects)})-(\d+)\b")
    return pattern.sub(
        rf'<a href="https://{domain}/browse/\1-\2">\1-\2</a>',
        escaped_text,
    )


def vehicle_sort_key(name):
    return int(name.split("-")[1])


def table_with_operator(items):
    rows = ['<table><tbody>',
            '<tr><th>Vehicle</th><th>Activity</th><th>Operator</th>'
            '<th>Location</th><th>Notes</th></tr>']
    for name, v in items:
        rows.append(
            f'<tr><td><p>{name}</p></td>'
            f'<td>{cell(v["activity"])}</td>'
            f'<td>{cell(v["operator"] or "Unassigned")}</td>'
            f'<td>{cell(v["location"])}</td>'
            f'<td>{cell(v["notes"])}</td></tr>'
        )
    rows.append('</tbody></table>')
    return "\n".join(rows)


def table_without_operator(items):
    rows = ['<table><tbody>',
            '<tr><th>Vehicle</th><th>Activity</th><th>Location</th><th>Notes</th></tr>']
    for name, v in items:
        rows.append(
            f'<tr><td><p>{name}</p></td>'
            f'<td>{cell(v["activity"])}</td>'
            f'<td>{cell(v["location"])}</td>'
            f'<td>{cell(v["notes"])}</td></tr>'
        )
    rows.append('</tbody></table>')
    return "\n".join(rows)


def items_for(vehicles, status):
    return sorted(
        [(n, v) for n, v in vehicles.items() if v["status"] == status],
        key=lambda kv: vehicle_sort_key(kv[0]),
    )


def build_body(vehicles, changes, rate_limited):
    counts = Counter(v["status"] for v in vehicles.values())

    parts = ['<h2>Summary</h2>',
             '<table><tbody>',
             '<tr><th>Status</th><th>Count</th></tr>']
    for status in ["Active", "Verification", "Validation", "Calibration",
                   "Build", "Out of Service", "Inactive"]:
        parts.append(
            f'<tr><td>{STATUS_EMOJI.get(status, "")} {status}</td>'
            f'<td>{counts.get(status, 0)}</td></tr>'
        )
    parts.append(
        f'<tr><td><strong>Total</strong></td>'
        f'<td><strong>{len(vehicles)}</strong></td></tr>'
    )
    parts.append('</tbody></table>')

    actives = items_for(vehicles, "Active")
    active_total = len(actives)
    active_assigned = sum(1 for _, v in actives if v["operator"])
    active_unassigned = active_total - active_assigned
    pct_a = round(100 * active_assigned / active_total) if active_total else 0
    pct_u = 100 - pct_a if active_total else 0
    parts.append(
        f'<h2>🟢 Active ({active_total}) — {active_assigned} assigned ({pct_a}%) '
        f'| {active_unassigned} unassigned ({pct_u}%)</h2>'
    )
    parts.append(table_with_operator(actives))

    for status, include_op in [("Verification", False),
                               ("Validation", False),
                               ("Calibration", True)]:
        items = items_for(vehicles, status)
        if items:
            parts.append(f'<h2>{STATUS_EMOJI[status]} {status} ({len(items)})</h2>')
            parts.append(table_with_operator(items) if include_op
                         else table_without_operator(items))

    if counts.get("Build"):
        items = items_for(vehicles, "Build")
        parts.append(f'<h2>🟣 Build ({len(items)})</h2>')
        parts.append(table_without_operator(items))

    if counts.get("Out of Service"):
        items = items_for(vehicles, "Out of Service")
        parts.append(f'<h2>🔴 Out of Service ({len(items)})</h2>')
        parts.append(table_with_operator(items))

    if counts.get("Inactive"):
        items = items_for(vehicles, "Inactive")
        parts.append(f'<h2>🔘 Inactive ({len(items)})</h2>')
        parts.append(table_without_operator(items))

    parts.append('<h2>Latest Changes</h2>')
    parts.append('<ul>')
    if not changes["status"] and not changes["notes"]:
        parts.append('<li>No status transitions, no ticket/notes changes</li>')
    else:
        for name, old, new in changes["status"]:
            parts.append(
                f'<li><strong>{name}</strong>: '
                f'{STATUS_EMOJI.get(old, "")} {html.escape(old)} → '
                f'{STATUS_EMOJI.get(new, "")} {html.escape(new)}</li>'
            )
        for name, old, new in changes["notes"]:
            old_disp = html.escape(old or "(empty)", quote=False)
            new_disp = linkify(html.escape(new, quote=False))
            parts.append(
                f'<li><strong>{name}</strong> notes: '
                f'<code>{old_disp}</code> → <code>{new_disp}</code></li>'
            )
    if rate_limited:
        parts.append(
            '<li>Fleetio assignment endpoint rate-limited — used driver-field fallback</li>'
        )
    parts.append(
        f'<li>Generated automatically '
        f'{html.escape(datetime.now(SCRIPT_TZ).strftime("%Y-%m-%d %H:%M %Z"))}</li>'
    )
    parts.append('</ul>')

    return "\n".join(parts)


def confluence_auth():
    raw = f"{env('ATLASSIAN_EMAIL')}:{env('ATLASSIAN_API_TOKEN')}"
    return "Basic " + base64.b64encode(raw.encode()).decode()


def get_current_page():
    url = f"https://{env('ATLASSIAN_DOMAIN')}/wiki/api/v2/pages/{env('CONFLUENCE_PAGE_ID')}"
    r = requests.get(
        url,
        headers={"Authorization": confluence_auth(), "Accept": "application/json"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def update_page(body_storage, title, version_number, version_message):
    domain = env("ATLASSIAN_DOMAIN")
    page_id = env("CONFLUENCE_PAGE_ID")
    url = f"https://{domain}/wiki/api/v2/pages/{page_id}"
    payload = {
        "id": page_id,
        "status": "current",
        "title": title,
        "body": {"representation": "storage", "value": body_storage},
        "version": {"number": version_number, "message": version_message},
    }
    r = requests.put(
        url,
        headers={
            "Authorization": confluence_auth(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json=payload,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def main():
    print("Pulling Fleetio data...", file=sys.stderr)
    vehicles, disagreements, rate_limited = pull_fleetio_state()
    print(f"  {len(vehicles)} vehicles, {len(disagreements)} disagreements",
          file=sys.stderr)

    prev_vehicles = load_prev_state()
    changes = build_changes(prev_vehicles, vehicles)

    today = datetime.now(SCRIPT_TZ).strftime("%Y-%m-%d")
    current_page = get_current_page()
    prev_title = current_page["title"]
    prev_version = current_page["version"]["number"]

    new_title = f"All DC Fleet - Daily Status Report {today}"
    date_changed = prev_title != new_title

    skip_if_only_ops = env("SKIP_IF_ONLY_OPERATORS", "1", required=False) == "1"
    only_operator_changes = not changes["status"] and not changes["notes"]

    if skip_if_only_ops and only_operator_changes and not date_changed and prev_vehicles:
        print("No status/notes/title changes — skipping republish", file=sys.stderr)
        return 0

    body = build_body(vehicles, changes, rate_limited)

    if env("DRY_RUN", "0", required=False) == "1":
        print(body)
        return 0

    msg_parts = []
    if changes["status"]:
        msg_parts.append(f"{len(changes['status'])} status change(s)")
    if changes["notes"]:
        msg_parts.append(f"{len(changes['notes'])} notes/ticket change(s)")
    if not msg_parts:
        msg_parts.append("date rollover" if date_changed else "auto refresh")
    version_msg = "Auto: " + "; ".join(msg_parts)

    print(f"Publishing v{prev_version + 1}: {version_msg}", file=sys.stderr)
    update_page(body, new_title, prev_version + 1, version_msg)
    save_state(vehicles)
    return 0


if __name__ == "__main__":
    sys.exit(main())
