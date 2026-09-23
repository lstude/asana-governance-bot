"""
sync/notion_sync.py — refresh the PD-SET table in the Notion governance doc.

The page carries a marker pair as two plain paragraph blocks:

    {{PD-SET:BEGIN}}
    {{PD-SET:END}}

(Curly-brace tokens, not HTML comments: Notion auto-converts `-->` into an
arrow, which corrupted the old `<!-- ... -->` markers. `{{...}}` is left alone.)

On each run we:
  1. Locate the BEGIN / END markers by exact text, recursing INTO toggles and
     other container blocks — the governance sections live inside toggles.
  2. Delete (archive) every block strictly between them.
  3. Insert a "Last synced" callout + a fresh PD- table right after BEGIN, as a
     child of whatever container holds the markers (the toggle, or the page).

Everything outside the markers (the written naming spec, examples, request
workflow) is never touched. If the markers are missing, we log and skip rather
than guessing where the table goes.

Sprint 1 syncs PD-SET only. TEAM-LIBRARIES / TEMPLATES-LIVE / PORTFOLIOS-LIVE
are wired in later sprints using the same machinery.
"""

from __future__ import annotations

import json
import time
from urllib import request, error, parse

NOTION_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

PD_SET_BEGIN = "{{PD-SET:BEGIN}}"
PD_SET_END = "{{PD-SET:END}}"

# How deep to recurse looking for markers (page → toggle → ... ). Generous.
MAX_MARKER_DEPTH = 6

PD_TABLE_COLUMNS = ["Field name", "Type", "Project count", "Values"]


class NotionError(RuntimeError):
    pass


class NotionClient:
    def __init__(self, token: str, max_retries: int = 5, base_delay: float = 1.0):
        self.token = token
        self.max_retries = max_retries
        self.base_delay = base_delay

    def _request(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        qs = ("?" + parse.urlencode(params)) if params else ""
        url = f"{NOTION_BASE}{path}{qs}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": NOTION_VERSION,
            "Accept": "application/json",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        last_err = None
        for attempt in range(self.max_retries):
            req = request.Request(url, data=data, method=method, headers=headers)
            try:
                with request.urlopen(req, timeout=30) as r:
                    return json.loads(r.read().decode("utf-8"))
            except error.HTTPError as e:
                code = e.code
                if code == 429 or 500 <= code < 600:
                    retry_after = e.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after else self.base_delay * (2 ** attempt)
                    last_err = NotionError(f"{method} {path} -> HTTP {code}")
                    time.sleep(delay)
                    continue
                detail = e.read().decode("utf-8", errors="replace")
                raise NotionError(f"{method} {path} -> HTTP {code}: {detail}") from e
            except Exception as e:
                last_err = e
                time.sleep(self.base_delay * (2 ** attempt))
        raise NotionError(f"{method} {path} failed after {self.max_retries} attempts: {last_err}")

    def list_children(self, block_id: str) -> list:
        results, cursor = [], None
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            resp = self._request("GET", f"/blocks/{block_id}/children", params=params)
            results.extend(resp.get("results", []))
            if resp.get("has_more"):
                cursor = resp.get("next_cursor")
            else:
                break
        return results

    def append_children(self, block_id: str, children: list, after: str | None = None):
        body = {"children": children}
        if after:
            body["after"] = after
        return self._request("PATCH", f"/blocks/{block_id}/children", body=body)

    def delete_block(self, block_id: str):
        return self._request("DELETE", f"/blocks/{block_id}")


# --- block helpers ----------------------------------------------------------
def _plain_text(block: dict) -> str:
    """Concatenate the plain text of a paragraph/heading block, trimmed."""
    btype = block.get("type")
    payload = block.get(btype) or {}
    rich = payload.get("rich_text") or []
    return "".join(rt.get("plain_text", "") for rt in rich).strip()


def _rt(text: str) -> list:
    # Notion caps a single text run at 2000 chars — split long text into runs.
    text = text or ""
    if len(text) <= 1900:
        return [{"type": "text", "text": {"content": text}}]
    return [{"type": "text", "text": {"content": text[i:i + 1900]}}
            for i in range(0, len(text), 1900)]


def _cell(text: str) -> list:
    return _rt(text) if text else []


def _callout(text: str) -> dict:
    return {
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": _rt(text),
            "icon": {"type": "emoji", "emoji": "🔄"},
            "color": "gray_background",
        },
    }


def _table_row(row: list) -> dict:
    return {"object": "block", "type": "table_row",
            "table_row": {"cells": [_cell(c) for c in row]}}


def _table(rows: list[list[str]]) -> dict:
    """rows[0] is the header row. NOTE: Notion accepts at most 100 children when
    creating a table in one request — callers with more rows must append the rest
    to the table block afterward (see sync_table_section)."""
    return {
        "object": "block",
        "type": "table",
        "table": {
            "table_width": len(rows[0]),
            "has_column_header": True,
            "has_row_header": False,
            "children": [_table_row(r) for r in rows],
        },
    }


def _pd_rows(inventory: list) -> tuple[list[list[str]], int]:
    """Return (rows, pd_count). rows[0] is the header; one row per PD- field
    found in the inventory (reality, not the aspirational list). Sorted by name;
    project count and values come from the pull."""
    pd_fields = [
        r for r in inventory
        if (r.get("name_stripped") or r.get("name") or "").strip().upper().startswith("PD-")
    ]
    pd_fields.sort(key=lambda r: (r.get("name_stripped") or r.get("name") or "").lower())
    rows = [list(PD_TABLE_COLUMNS)]
    for r in pd_fields:
        name = (r.get("name_stripped") or r.get("name") or "").strip()
        ftype = (r.get("type") or "").strip()
        count = str(r.get("on_project_count") or 0)
        values = (r.get("options") or "").strip()
        rows.append([name, ftype, count, values])
    return rows, len(pd_fields)


def find_marker_container(client: NotionClient, root_id: str, begin_text: str, end_text: str,
                          _depth: int = 0):
    """
    Search the block tree under root_id for a container whose DIRECT children
    include both markers (as paragraph blocks). Recurses into any block that has
    children (toggles, columns, etc.) so markers nested in a toggle are found.

    Returns (parent_id, children, begin_idx, end_idx) or None.
    Both markers must live in the SAME container, in order.
    """
    children = client.list_children(root_id)
    begin_idx = end_idx = None
    for i, b in enumerate(children):
        if b.get("type") == "paragraph":
            txt = _plain_text(b)
            if txt == begin_text:
                begin_idx = i
            elif txt == end_text:
                end_idx = i
    if begin_idx is not None and end_idx is not None and end_idx > begin_idx:
        return root_id, children, begin_idx, end_idx

    if _depth < MAX_MARKER_DEPTH:
        for b in children:
            if b.get("has_children"):
                found = find_marker_container(client, b["id"], begin_text, end_text, _depth + 1)
                if found:
                    return found
    return None


def markers_present(client: NotionClient, page_id: str,
                    begin_text: str = PD_SET_BEGIN, end_text: str = PD_SET_END) -> bool:
    """Read-only: True if the marker pair exists anywhere in the page tree.
    Used by the preflight check."""
    return find_marker_container(client, page_id, begin_text, end_text) is not None


def _replace_between_markers(client: NotionClient, page_id: str, begin_text: str,
                             end_text: str, new_children: list) -> dict | None:
    """Find a marker pair (anywhere, incl. toggles), delete blocks between them,
    and insert new_children right after BEGIN in the same container.
    Returns {"deleted_blocks", "container"} or None if markers are absent."""
    found = find_marker_container(client, page_id, begin_text, end_text)
    if not found:
        return None
    parent_id, children, begin_idx, end_idx = found
    begin_block = children[begin_idx]
    between = children[begin_idx + 1:end_idx]
    for b in between:
        client.delete_block(b["id"])
    if new_children:
        client.append_children(parent_id, new_children, after=begin_block["id"])
    return {"deleted_blocks": len(between), "container": parent_id}


def sync_pd_set(client: NotionClient, page_id: str, inventory: list, run_date: str) -> dict:
    """Refresh the PD-SET marked region. Returns a status dict; never raises on
    a missing marker (logs and skips so the rest of the run proceeds)."""
    rows, pd_count = _pd_rows(inventory)
    callout = _callout(f"Last synced from Asana: {run_date}. PD-: {pd_count} fields. "
                       f"(Auto-generated — edits here are overwritten every Monday.)")
    res = _replace_between_markers(client, page_id, PD_SET_BEGIN, PD_SET_END,
                                   [callout, _table(rows)])
    if res is None:
        return {"status": "skipped",
                "reason": "PD-SET markers not found anywhere in the page tree; "
                          "paste the snippet from config/notion_markers.md (inside the "
                          "Custom field naming standards toggle is fine)"}
    return {"status": "synced", "pd_count": pd_count, **res}


# --- team libraries (Sprint 2) ----------------------------------------------
TEAM_LIB_BEGIN = "{{TEAM-LIBRARIES:BEGIN}}"
TEAM_LIB_END = "{{TEAM-LIBRARIES:END}}"


def _toggle(title: str, children: list) -> dict:
    return {"object": "block", "type": "toggle",
            "toggle": {"rich_text": _rt(title), "children": children}}


def _prefix_of(name: str) -> str | None:
    stripped = (name or "").strip()
    return stripped.split("-", 1)[0].strip() if "-" in stripped else None


def _team_rows(inventory: list, prefix: str, asana_only: bool) -> list[list[str]]:
    """Header + one row per field whose prefix matches, filtered by system/user."""
    rows = [list(PD_TABLE_COLUMNS)]
    matched = []
    for r in inventory:
        name = (r.get("name_stripped") or r.get("name") or "").strip()
        if (_prefix_of(name) or "").upper() != prefix.upper():
            continue
        is_asana = (r.get("created_by_is_asana") or "").strip().lower() == "true"
        if is_asana != asana_only:
            continue
        matched.append((name, (r.get("type") or "").strip(),
                        str(r.get("on_project_count") or 0), (r.get("options") or "").strip()))
    matched.sort(key=lambda x: x[0].lower())
    rows.extend([list(m) for m in matched])
    return rows


def sync_team_libraries(client: NotionClient, page_id: str, inventory: list,
                        team_prefixes: list, run_date: str) -> dict:
    """Refresh the TEAM-LIBRARIES region: one collapsible table per team prefix
    that has at least one user-created field, plus an ASA- reference toggle.
    ASA- fields are reference-only (never tasked), per the spec."""
    children = []
    counts = {}
    for prefix in team_prefixes:
        rows = _team_rows(inventory, prefix, asana_only=False)
        n = len(rows) - 1
        if n == 0:
            continue
        counts[prefix] = n
        children.append(_toggle(f"{prefix}- ({n} field{'s' if n != 1 else ''})", [_table(rows)]))

    asa_rows = _team_rows(inventory, "ASA", asana_only=True)
    asa_n = len(asa_rows) - 1
    if asa_n:
        children.append(_toggle(f"ASA- ({asa_n} Asana system fields — reference only)", [_table(asa_rows)]))

    summary = ", ".join(f"{p}-: {c}" for p, c in counts.items()) or "no team fields"
    callout = _callout(f"Last synced from Asana: {run_date}. {summary}. "
                       f"(Auto-generated — edits here are overwritten every Monday.)")
    res = _replace_between_markers(client, page_id, TEAM_LIB_BEGIN, TEAM_LIB_END,
                                   [callout] + children)
    if res is None:
        return {"status": "skipped",
                "reason": "TEAM-LIBRARIES markers not found; paste the snippet from "
                          "config/notion_markers.md"}
    return {"status": "synced", "teams": counts, "asa_fields": asa_n, **res}


# --- generic table section (templates / portfolios, Sprint 2) ---------------
TEMPLATES_LIVE_BEGIN = "{{TEMPLATES-LIVE:BEGIN}}"
TEMPLATES_LIVE_END = "{{TEMPLATES-LIVE:END}}"
PORTFOLIOS_LIVE_BEGIN = "{{PORTFOLIOS-LIVE:BEGIN}}"
PORTFOLIOS_LIVE_END = "{{PORTFOLIOS-LIVE:END}}"


_TABLE_MAX = 100   # Notion: max children when creating a table in one request


def sync_table_section(client: NotionClient, page_id: str, begin_text: str, end_text: str,
                       columns: list, rows: list, run_date: str, summary: str,
                       flagged_lines: list | None = None) -> dict:
    """Replace a marked region with a "Last synced" callout, an optional
    "Flagged for review" callout, and a table (header = columns). Handles Notion's
    limits: long text is split into runs (_rt), and tables over 100 rows are
    created with the first 100 then extended in batches."""
    found = find_marker_container(client, page_id, begin_text, end_text)
    if not found:
        return {"status": "skipped",
                "reason": f"markers not found ({begin_text}); paste from config/notion_markers.md"}
    parent_id, children, b_idx, e_idx = found
    anchor = children[b_idx]["id"]
    for b in children[b_idx + 1:e_idx]:
        client.delete_block(b["id"])

    lead = [_callout(f"Last synced from Asana: {run_date}. {summary}. "
                     f"(Auto-generated — edits here are overwritten every Monday.)")]
    if flagged_lines:
        lead.append({"object": "block", "type": "callout",
                     "callout": {"rich_text": _rt("⚠️ Flagged for review:\n" + "\n".join(flagged_lines)),
                                 "icon": {"type": "emoji", "emoji": "⚠️"}, "color": "yellow_background"}})
    resp = client.append_children(parent_id, lead, after=anchor)
    res = resp.get("results") or []
    if res:
        anchor = res[-1]["id"]

    matrix = [list(columns)] + [list(r) for r in rows]
    first, rest = matrix[:_TABLE_MAX], matrix[_TABLE_MAX:]
    resp = client.append_children(parent_id, [_table(first)], after=anchor)
    table_id = next((b["id"] for b in resp.get("results", []) if b.get("type") == "table"), None)
    for i in range(0, len(rest), _TABLE_MAX):
        if table_id:
            client.append_children(table_id, [_table_row(r) for r in rest[i:i + _TABLE_MAX]])

    return {"status": "synced", "rows": len(rows), "container": parent_id}
