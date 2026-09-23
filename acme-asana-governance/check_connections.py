#!/usr/bin/env python3
"""
check_connections.py — preflight smoke test for the three integrations.

Read-only by default: confirms the Asana token, the cleanup project + its
PD-Priority/PD-DRI fields, and the Notion integration's access + that the
PD-SET markers are pasted in the page. Touches nothing.

    python3 check_connections.py            # Asana + Notion (read-only)
    python3 check_connections.py --slack    # also POST a test Slack message

Reads the same env vars the live run uses:
    ASANA_SERVICE_TOKEN, ASANA_CLEANUP_PROJECT_GID,
    NOTION_TOKEN, NOTION_GOVERNANCE_PAGE_ID, SLACK_WEBHOOK_URL
"""

from __future__ import annotations

import argparse
import os
import sys

from asana.task_creator import AsanaClient, TaskCreator
from sync.notion_sync import NotionClient, markers_present
from slack import compose

OK, BAD, WARN = "✓", "✗", "•"


def _line(symbol, text):
    print(f"  {symbol} {text}")


def check_asana() -> bool:
    print("ASANA")
    token = os.environ.get("ASANA_SERVICE_TOKEN")
    project = os.environ.get("ASANA_CLEANUP_PROJECT_GID")
    if not token:
        _line(BAD, "ASANA_SERVICE_TOKEN not set")
        return False
    client = AsanaClient(token)
    ok = True
    try:
        me = client.get("/users/me", params={"opt_fields": "name,email"}).get("data", {})
        _line(OK, f"token valid — authenticated as {me.get('name')} ({me.get('email')})")
    except Exception as e:
        _line(BAD, f"token check failed: {e}")
        return False

    if not project:
        _line(BAD, "ASANA_CLEANUP_PROJECT_GID not set")
        return False
    try:
        proj = client.get(f"/projects/{project}", params={"opt_fields": "name"}).get("data", {})
        _line(OK, f"cleanup project reachable — \"{proj.get('name')}\"")
    except Exception as e:
        _line(BAD, f"cleanup project {project} not reachable: {e}")
        return False

    # Confirm the custom fields the task creator stamps actually exist here.
    try:
        creator = TaskCreator(client, project)
        cf = creator._resolve_custom_fields()
        if cf["priority_gid"]:
            has_med = "Medium" in cf["priority_options"]
            _line(OK if has_med else WARN,
                  f"PD-Priority present" + ("" if has_med else " — but no 'Medium' option (check value name)"))
            ok = ok and has_med
        else:
            _line(WARN, "PD-Priority not on the cleanup project — tasks will be created without a priority")
        if cf["dri_gid"]:
            _line(OK, "PD-DRI present")
        else:
            _line(WARN, "PD-DRI not on the cleanup project — tasks will be created without a DRI")
    except Exception as e:
        _line(BAD, f"could not read cleanup project custom fields: {e}")
        ok = False
    return ok


def check_notion() -> bool:
    print("NOTION")
    token = os.environ.get("NOTION_TOKEN")
    page = os.environ.get("NOTION_GOVERNANCE_PAGE_ID")
    if not token or not page:
        _line(BAD, "NOTION_TOKEN or NOTION_GOVERNANCE_PAGE_ID not set")
        return False
    client = NotionClient(token)
    try:
        children = client.list_children(page)
        _line(OK, f"integration can read the governance page ({len(children)} top-level blocks)")
    except Exception as e:
        _line(BAD, f"cannot read page {page}: {e} (is the page shared with the integration?)")
        return False

    try:
        if markers_present(client, page):
            _line(OK, "PD-SET markers found (incl. inside toggles) — Notion sync will run")
            return True
    except Exception as e:
        _line(BAD, f"error while searching for markers: {e}")
        return False
    _line(BAD, "PD-SET markers not found — paste the snippet from config/notion_markers.md")
    return False


def check_slack(send: bool) -> bool:
    print("SLACK")
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        _line(WARN, "SLACK_WEBHOOK_URL not set locally (it's fine if it's only in GitHub Secrets)")
        return True
    if not send:
        _line(OK, "SLACK_WEBHOOK_URL is set — re-run with --slack to send a test message")
        return True
    try:
        compose.post(url, {"text": "✅ pd-asana-governance preflight: webhook is wired up correctly."})
        _line(OK, "test message posted — check the #asana-governance channel")
        return True
    except Exception as e:
        _line(BAD, f"webhook post failed: {e}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Preflight check for Asana/Notion/Slack.")
    ap.add_argument("--slack", action="store_true", help="send a test Slack message (a real post)")
    args = ap.parse_args()

    results = [check_asana(), print() or check_notion(), print() or check_slack(args.slack)]
    print()
    if all(results):
        print("All checks passed — safe to do a live run.")
        return 0
    print("Some checks failed — fix the ✗ items above before the live run.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
