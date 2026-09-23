#!/usr/bin/env python3
"""
pull_inventory_complete.py  (v2)

Pulls EVERY custom field in the workspace, two ways:

  1. Workspace globals via /workspaces/{wid}/custom_fields
     This is the field library you see in the Asana UI — but includes orphans
     (global per the API but not visible in the library UI).

  2. Project-scoped fields via walking every project and reading its
     custom_field_settings. These don't show in the library because they only
     exist on one project.

NEW IN V2:
  - `likely_in_library_ui` column — heuristic for whether the field shows
    in Asana's library UI (global + on_project_count > 0). Useful for
    explaining the gap between API-visible and UI-visible counts.
  - `name_has_whitespace` column — flags names with leading/trailing spaces
    so you can clean them up (e.g. "Ops Launch Status " with trailing space).
  - `name_stripped` column — the trimmed version, for cleaner display.

Usage:
    export ASANA_SERVICE_TOKEN="0/your-personal-admin-token"
    cd ~/pd-asana-cleanup
    python3 pull_inventory_complete.py

Output: inventory_complete_<timestamp>.csv
"""
import csv
import json
import os
import sys
import time
from datetime import datetime
from urllib import request, error, parse

TOKEN = os.environ.get("ASANA_SERVICE_TOKEN") or os.environ.get("ASANA_TOKEN")
if not TOKEN:
    print("ERROR: set ASANA_SERVICE_TOKEN environment variable")
    sys.exit(1)

WORKSPACE_GID = "544529763028279"
BASE = "https://app.asana.com/api/1.0"

OPT_FIELDS_FIELD = ",".join([
    "gid", "name", "type", "resource_subtype",
    "is_global_to_workspace",
    "created_by.gid", "created_by.name",
    "description",
    "enum_options.name", "enum_options.enabled",
    "multi_enum_options.name", "multi_enum_options.enabled",
    "custom_label", "custom_label_position",
    "format", "precision",
])


def api_get(path, params=None):
    qs = ("?" + parse.urlencode(params)) if params else ""
    url = f"{BASE}{path}{qs}"
    req = request.Request(url, method="GET",
        headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"})
    try:
        with request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", errors="replace"))
        except Exception:
            return e.code, {"error": "non-json"}
    except Exception as e:
        return None, {"error": f"{type(e).__name__}: {e}"}


def options_string(field):
    opts = field.get("enum_options") or field.get("multi_enum_options") or []
    return " | ".join(o.get("name", "") for o in opts if o.get("enabled", True))


def normalize_field(f, scope, on_project_count=0, host=("", "")):
    created_by = f.get("created_by") or {}
    created_by_name = created_by.get("name") if created_by else ""
    created_by_gid = created_by.get("gid") if created_by else ""
    created_by_is_asana = (
        not created_by_name or
        (created_by_name.lower() == "asana") or
        (created_by_gid == "0")
    )
    description = f.get("description") or ""
    has_asana_desc = "asana-created" in description.lower()
    raw_name = f.get("name") or ""
    stripped_name = raw_name.strip()
    has_whitespace = (raw_name != stripped_name) and bool(raw_name)
    is_global = bool(f.get("is_global_to_workspace", False))

    # Library-UI visibility heuristic:
    # - Must be global per the API
    # - Asana-owned globals always show (they're system defaults in the picker)
    # - User-owned globals only show if they're on at least one project
    if not is_global:
        likely_in_library = False
    elif created_by_is_asana:
        likely_in_library = True
    else:
        likely_in_library = on_project_count > 0

    return {
        "gid": f.get("gid", ""),
        "name": raw_name,
        "name_stripped": stripped_name,
        "name_has_whitespace": has_whitespace,
        "type": f.get("type") or "",
        "resource_subtype": f.get("resource_subtype") or "",
        "scope": scope,
        "is_global_to_workspace": is_global,
        "likely_in_library_ui": likely_in_library,
        "created_by": created_by_name or "",
        "created_by_gid": created_by_gid or "",
        "created_by_is_asana": created_by_is_asana,
        "option_count": len(f.get("enum_options") or f.get("multi_enum_options") or []),
        "options": options_string(f),
        "description": description,
        "has_asana_created_description": has_asana_desc,
        "custom_label": f.get("custom_label") or "",
        "custom_label_position": f.get("custom_label_position") or "",
        "on_project_count": on_project_count,
        "host_project_gid": host[0] or "",
        "host_project_name": host[1] or "",
    }


def fetch_workspace_global_fields():
    print("Step 1: Fetching workspace-global custom fields...")
    fields = []
    offset = None
    page = 0
    while True:
        page += 1
        params = {"limit": 100, "opt_fields": OPT_FIELDS_FIELD}
        if offset:
            params["offset"] = offset
        status, body = api_get(f"/workspaces/{WORKSPACE_GID}/custom_fields", params=params)
        if status != 200:
            print(f"  ERROR on page {page}: {status}")
            if page == 1:
                return None
            break
        data = body.get("data", [])
        fields.extend(data)
        print(f"  page {page}: {len(data)} fields (running total: {len(fields)})")
        next_page = body.get("next_page")
        if next_page and next_page.get("offset"):
            offset = next_page["offset"]
        else:
            break
        time.sleep(0.15)
    print(f"  ✓ Got {len(fields)} workspace-global fields\n")
    return fields


def fetch_all_projects():
    print("Step 2: Listing all projects to find project-scoped fields...")
    projects = []
    offset = None
    page = 0
    while True:
        page += 1
        params = {
            "workspace": WORKSPACE_GID, "limit": 100,
            "opt_fields": "gid,name,archived"
        }
        if offset:
            params["offset"] = offset
        status, body = api_get("/projects", params=params)
        if status != 200:
            print(f"  ERROR on page {page}: {status}")
            break
        projects.extend(body.get("data", []))
        next_page = body.get("next_page")
        if next_page and next_page.get("offset"):
            offset = next_page["offset"]
        else:
            break
        time.sleep(0.1)
    print(f"  ✓ Found {len(projects)} projects\n")
    return projects


def fetch_project_scoped_fields(projects, known_global_gids):
    print(f"Step 3: Scanning {len(projects)} projects for project-scoped fields...")
    field_records = {}
    field_project_count = {}
    field_host = {}   # gid -> (project_gid, project_name) of the first project it's on
    errors = 0
    for i, p in enumerate(projects, 1):
        if i % 100 == 0:
            print(f"  scanned {i}/{len(projects)} projects... ({len(field_records)} project-scoped fields found so far)")
        status, body = api_get(
            f"/projects/{p['gid']}/custom_field_settings",
            params={"opt_fields": f"custom_field.{OPT_FIELDS_FIELD.replace(',', ',custom_field.')}"},
        )
        if status != 200:
            errors += 1
            time.sleep(0.1)
            continue
        for setting in body.get("data", []):
            cf = setting.get("custom_field") or {}
            gid = cf.get("gid")
            if not gid:
                continue
            field_project_count[gid] = field_project_count.get(gid, 0) + 1
            if gid not in field_host:
                field_host[gid] = (p.get("gid", ""), p.get("name", ""))
            if gid in known_global_gids:
                continue
            if gid not in field_records:
                field_records[gid] = cf
        time.sleep(0.08)
    print(f"  ✓ Found {len(field_records)} project-scoped fields not in the global library")
    print(f"    ({errors} project scan errors)\n")
    return field_records, field_project_count, field_host


def main():
    print(f"Token (first 8): {TOKEN[:8]}...\n")
    print("=" * 70)

    global_fields_raw = fetch_workspace_global_fields()
    global_gids = set()
    global_records = []
    if global_fields_raw:
        for f in global_fields_raw:
            global_gids.add(f.get("gid"))
            global_records.append(f)

    projects = fetch_all_projects()
    project_scoped_raw, project_counts, field_host = fetch_project_scoped_fields(projects, global_gids)

    all_rows = []
    for f in global_records:
        count = project_counts.get(f.get("gid"), 0)
        all_rows.append(normalize_field(f, scope="global", on_project_count=count,
                                        host=field_host.get(f.get("gid"), ("", ""))))
    for gid, f in project_scoped_raw.items():
        count = project_counts.get(gid, 0)
        all_rows.append(normalize_field(f, scope="project-scoped", on_project_count=count,
                                        host=field_host.get(gid, ("", ""))))

    all_rows.sort(key=lambda r: (0 if r["scope"] == "global" else 1, r["name_stripped"].lower()))

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = f"inventory_complete_{ts}.csv"
    fieldnames = [
        "gid", "name", "name_stripped", "name_has_whitespace",
        "type", "resource_subtype", "scope",
        "is_global_to_workspace", "likely_in_library_ui",
        "created_by", "created_by_gid", "created_by_is_asana",
        "option_count", "options", "description",
        "has_asana_created_description", "custom_label",
        "custom_label_position", "on_project_count",
        "host_project_gid", "host_project_name",
    ]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in all_rows:
            w.writerow(row)

    print("=" * 70)
    print(f"\n✓ Wrote: {out_path}\n")
    print(f"Summary:")
    print(f"  Total fields:                  {len(all_rows)}")
    print(f"  Global (per API):              {sum(1 for r in all_rows if r['scope']=='global')}")
    print(f"  Project-scoped:                {sum(1 for r in all_rows if r['scope']=='project-scoped')}")
    print(f"  Likely visible in UI library:  {sum(1 for r in all_rows if r['likely_in_library_ui'])}")
    print(f"  NOT visible in UI library:     {sum(1 for r in all_rows if not r['likely_in_library_ui'])}")
    print(f"  Asana-owned (system):          {sum(1 for r in all_rows if r['created_by_is_asana'])}")
    print(f"  User-owned:                    {sum(1 for r in all_rows if not r['created_by_is_asana'])}")
    print(f"  Names with whitespace issue:   {sum(1 for r in all_rows if r['name_has_whitespace'])}")
    print(f"  Orphans (on 0 projects):       {sum(1 for r in all_rows if r['on_project_count']==0)}")


if __name__ == "__main__":
    main()
