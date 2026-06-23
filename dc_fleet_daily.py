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
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

FLEETIO_BASE = "https://secure.fleetio.com/api/v1"
SCRIPT_TZ = ZoneInfo("America/Los_Angeles")


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
    """Paginate /vehicles or /vehicle_assignments. Stops on rate-limit."""
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
    """Returns {ROG-NNN: {status, activity, location, notes, operator, id}}."""
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
    """Persists only {status, notes} per vehicle — no operator names."""
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


def linkify_tickets(text):
    """Wrap PROJ-NNN refs in markdown links to Atlassian."""
    domain = env("ATLASSIAN_DOMAIN")
    projects = env("JIRA_PROJECTS", "VSTAB,VBUILD,VCO,AVP", required=False).split(",")
    if not text:
        return text
    pattern = re.compile(rf"\b({'|'.join(projects)})-(\d+)\b")
    return pattern.sub(rf"[\1-\2](https://{domain}/browse/\1-\2)", text)


def escape_pipes(text):
    if not text:
        return ""
    return text.replace("|", "\\|")


def build_changes(prev, curr):
    """Returns dict of: status_transitions, notes_changes, operator_change_count."""
    out = {"status": [], "notes": [], "ops": 0}
    prev_names = set(prev.keys())
    curr_names = set(curr.keys())
    for name in sorted(curr_names & prev_names, key=lambda n: int(n.split("-")[1])):
        p = prev[name]
        c = curr[name]
        if p["status"] != c["status"]:
            out["status"].append((name, p["status"], c["status"]))
        if (p.get("notes") or "").strip() != (c["notes"] or "").strip():
            out["notes"].append((name, p.get("notes") or "", c["notes"]))
    return out


def format_active_table(vehicles):
    actives = sorted(
        [(n, v) for n, v in vehicles.items() if v["status"] == "Active"],
        key=lambda kv: int(kv[0].split("-")[1]),
    )
    rows = ["| Vehicle | Activity | Operator | Location | Notes |", "| --- | --- | --- | --- | --- |"]
    for name, v in actives:
        rows.append(
            f"| {name} | {v['activity']} | {v['operator'] or 'Unassigned'} | "
            f"{v['location']} | {escape_pipes(linkify_tickets(v['notes']))} |"
        )
    return "\n".join(rows), len(actives), sum(1 for _, v in actives if v["operator"])


def format_grouped_table(vehicles, status, include_operator=True):
    items = sorted(
        [(n, v) for n, v in vehicles.items() if v["status"] == status],
        key=lambda kv: int(kv[0].split("-")[1]),
    )
    if not items:
        return ""
    if include_operator:
        rows = [
            "| Vehicle | Activity | Operator | Location | Notes |",
            "| --- | --- | --- | --- | --- |",
        ]
        for name, v in items:
            rows.append(
                f"| {name} | {v['activity']} | {v['operator'] or 'Unassigned'} | "
                f"{v['location']} | {escape_pipes(linkify_tickets(v['notes']))} |"
            )
    else:
        rows = ["| Vehicle | Activity | Location | Notes |", "| --- | --- | --- | --- |"]
        for name, v in items:
            rows.append(
                f"| {name} | {v['activity']} | {v['location']} | "
                f"{escape_pipes(linkify_tickets(v['notes']))} |"
            )
    return "\n".join(rows)


STATUS_EMOJI = {
    "Active": "🟢",
    "Verification": "🟡",
    "Validation": "🟠",
    "Calibration": "🩷",
    "Build": "🟣",
    "Out of Service": "🔴",
    "Inactive": "🔘",
}


def build_body(vehicles, changes, rate_limited):
    counts = Counter(v["status"] for v in vehicles.values())
    summary_rows = [
        "| Status | Count |",
        "| --- | --- |",
    ]
    for status in ["Active", "Verification", "Validation", "Calibration", "Build", "Out of Service", "Inactive"]:
        emoji = STATUS_EMOJI.get(status, "")
        summary_rows.append(f"| {emoji} {status} | {counts.get(status, 0)} |")
    summary_rows.append(f"| **Total** | **{len(vehicles)}** |")

    active_table, active_total, active_assigned = format_active_table(vehicles)
    active_unassigned = active_total - active_assigned
    pct_a = round(100 * active_assigned / active_total) if active_total else 0
    pct_u = 100 - pct_a if active_total else 0

    parts = [
        "## Summary",
        "",
        "\n".join(summary_rows),
        "",
        f"## 🟢 Active ({active_total}) — {active_assigned} assigned ({pct_a}%) | {active_unassigned} unassigned ({pct_u}%)",
        "",
        active_table,
        "",
    ]

    for status in ["Verification", "Validation", "Calibration"]:
        if counts.get(status):
            tbl = format_grouped_table(vehicles, status, include_operator=(status == "Calibration"))
            parts += [f"## {STATUS_EMOJI[status]} {status} ({counts[status]})", "", tbl, ""]

    if counts.get("Build"):
        parts += [
            f"## 🟣 Build ({counts['Build']})",
            "",
            format_grouped_table(vehicles, "Build", include_operator=False),
            "",
        ]

    if counts.get("Out of Service"):
        parts += [
            f"## 🔴 Out of Service ({counts['Out of Service']})",
            "",
            format_grouped_table(vehicles, "Out of Service", include_operator=True),
            "",
        ]

    if counts.get("Inactive"):
        parts += [
            f"## 🔘 Inactive ({counts['Inactive']})",
            "",
            format_grouped_table(vehicles, "Inactive", include_operator=False),
            "",
        ]

    parts.append("## Latest Changes\n")
    if not changes["status"] and not changes["notes"]:
        parts.append("* No status transitions, no ticket/notes changes")
    else:
        for name, old, new in changes["status"]:
            o_emoji = STATUS_EMOJI.get(old, "")
            n_emoji = STATUS_EMOJI.get(new, "")
            parts.append(f"* **{name}**: {o_emoji} {old} → {n_emoji} {new}")
        for name, old, new in changes["notes"]:
            new_linked = escape_pipes(linkify_tickets(new))
            parts.append(f"* **{name}** notes: `{old or '(empty)'}` → `{new_linked}`")
    if rate_limited:
        parts.append("* Fleetio assignment endpoint rate-limited — used driver-field fallback")
    parts.append(
        f"* Generated automatically {datetime.now(SCRIPT_TZ).strftime('%Y-%m-%d %H:%M %Z')}"
    )

    return "\n".join(parts)


def confluence_auth():
    raw = f"{env('ATLASSIAN_EMAIL')}:{env('ATLASSIAN_API_TOKEN')}"
    return "Basic " + base64.b64encode(raw.encode()).decode()


def get_current_page():
    url = f"https://{env('ATLASSIAN_DOMAIN')}/wiki/api/v2/pages/{env('CONFLUENCE_PAGE_ID')}"
    r = requests.get(
        url,
        headers={"Authorization": confluence_auth(), "Accept": "application/json"},
        params={"body-format": "storage"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def update_page(body_markdown, title, version_number, version_message):
    """v2 API accepts storage representation. Convert markdown to storage via Atlassian's conversion endpoint."""
    domain = env("ATLASSIAN_DOMAIN")
    page_id = env("CONFLUENCE_PAGE_ID")

    convert_url = f"https://{domain}/wiki/api/v2/pages/{page_id}/content/convert"
    conv = requests.post(
        convert_url,
        headers={
            "Authorization": confluence_auth(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json={
            "value": body_markdown,
            "sourceContentFormat": "markdown",
            "targetContentFormat": "storage",
        },
        timeout=30,
    )
    if conv.status_code == 404:
        storage_value = markdown_to_storage_fallback(body_markdown)
    else:
        conv.raise_for_status()
        storage_value = conv.json().get("value", "")

    url = f"https://{domain}/wiki/api/v2/pages/{page_id}"
    payload = {
        "id": page_id,
        "status": "current",
        "title": title,
        "body": {"representation": "storage", "value": storage_value},
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


def markdown_to_storage_fallback(md):
    """Minimal markdown→XHTML for tables/headings/links. Used only if conversion endpoint unavailable."""
    import html
    out = []
    in_table = False
    in_header = False
    for raw_line in md.split("\n"):
        line = raw_line.rstrip()
        if line.startswith("## "):
            if in_table:
                out.append("</tbody></table>")
                in_table = False
            out.append(f"<h2>{html.escape(line[3:])}</h2>")
            continue
        if line.startswith("| ") and "|" in line[2:]:
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(set(c) <= set("- ") for c in cells):
                in_header = True
                continue
            if not in_table:
                out.append("<table><tbody>")
                in_table = True
            tag = "th" if in_header else "td"
            if in_header:
                in_header = False
            cell_html = "".join(f"<{tag}>{linkify_inline(c)}</{tag}>" for c in cells)
            out.append(f"<tr>{cell_html}</tr>")
            continue
        if in_table:
            out.append("</tbody></table>")
            in_table = False
        if line.startswith("* "):
            out.append(f"<p>{linkify_inline(line[2:])}</p>")
        elif line:
            out.append(f"<p>{linkify_inline(line)}</p>")
    if in_table:
        out.append("</tbody></table>")
    return "\n".join(out)


def linkify_inline(text):
    """Convert markdown [label](url) and **bold** in a cell."""
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    return text.replace("\\|", "|")


def main():
    print("Pulling Fleetio data...", file=sys.stderr)
    vehicles, disagreements, rate_limited = pull_fleetio_state()
    print(f"  {len(vehicles)} vehicles, {len(disagreements)} disagreements", file=sys.stderr)

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
