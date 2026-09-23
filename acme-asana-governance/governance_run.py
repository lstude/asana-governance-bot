#!/usr/bin/env python3
"""
governance_run.py — the weekly orchestrator (Sprint 1: custom fields).

Flow:
    1. Pull a fresh field inventory (or read one passed via --inventory).
    2. Diff user-created fields against last week's baseline.
    3. Classify new fields with the decision tree.
    4. File a pre-assigned cleanup task per actionable item (idempotent).
    5. Refresh the PD-SET table in Notion.
    6. Post the Slack digest.
    7. Write the new baseline so next Monday diffs against this week.

Runs LIVE by default (that's the point of an unattended cron). Use --dry-run
for local testing: it computes everything, writes the Slack JSON + inventory to
./artifacts, and writes NOTHING to Asana or Notion.

First run with no baseline = "establish baseline": snapshots state, files no
tasks, still refreshes Notion so the library reflects reality from day one.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import subprocess
import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import yaml

from checkers.fields import (
    FieldChecker, USE_EXISTING, RENAME, TEMPLATE_PROPAGATION, TRIAGE,
)
from checkers.templates import (
    TemplateChecker, TEMPLATE_FORK, TEMPLATE_REFORMAT, TEMPLATE_TRIAGE, TEMPLATE_COMPLIANT,
)
from checkers.portfolios import (
    PortfolioChecker, PORTFOLIO_METADATA, PORTFOLIO_REFORMAT, PORTFOLIO_COMPLIANT,
)
from checkers.projects import ProjectChecker, PROJECT_MISNAMED
from asana import task_templates
from asana.task_creator import AsanaClient, TaskCreator
from asana import pull_entities
from slack import compose
from sync.notion_sync import (
    NotionClient, sync_pd_set, sync_team_libraries, sync_table_section,
    TEMPLATES_LIVE_BEGIN, TEMPLATES_LIVE_END,
    PORTFOLIOS_LIVE_BEGIN, PORTFOLIOS_LIVE_END,
)

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config"
BASELINE_PATH = ROOT / "baseline" / "inventory_latest.json"
ARTIFACTS = ROOT / "artifacts"


# --- config / io ------------------------------------------------------------
def load_yaml(name: str) -> dict:
    with open(CONFIG / name, encoding="utf-8") as f:
        return yaml.safe_load(f)


def newest_inventory_csv() -> str | None:
    files = sorted(glob.glob(str(ROOT / "inventory_complete_*.csv")))
    return files[-1] if files else None


def pull_inventory() -> str:
    """Run the (untouched) inventory pull, then return the newest CSV path."""
    print("→ Pulling fresh inventory from Asana...")
    subprocess.run([sys.executable, str(ROOT / "pull_inventory_complete.py")],
                   cwd=str(ROOT), check=True)
    path = newest_inventory_csv()
    if not path:
        raise RuntimeError("inventory pull produced no CSV")
    return path


def read_inventory(path: str) -> list:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def is_user_created(row: dict) -> bool:
    return (row.get("created_by_is_asana") or "").strip().lower() != "true"


_ENTITY_KEYS = ("field_gids", "project_gids", "template_gids", "portfolio_gids")


def load_baseline() -> dict | None:
    """Return {run_date, field_gids:set, project_gids:set, ...} or None if absent.
    A missing/empty per-entity set means 'establish baseline' for that entity."""
    if not BASELINE_PATH.exists():
        return None
    try:
        data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    out = {"run_date": data.get("run_date")}
    for k in _ENTITY_KEYS:
        out[k] = set(data.get(k) or [])
    return out


def write_baseline(prev: dict | None, run_date: str, **entity_gids) -> None:
    """Write the baseline, updating only the entity sets passed this run and
    preserving the rest (so a fields-only run doesn't wipe the project baseline).
    Pass e.g. field_gids={...}, project_gids={...}."""
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    prev = prev or {}
    payload = {"run_date": run_date}
    for k in _ENTITY_KEYS:
        if entity_gids.get(k) is not None:
            payload[k] = sorted(str(g) for g in entity_gids[k] if g)
        else:
            payload[k] = sorted(prev.get(k) or [])
    BASELINE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


# --- task assignment policy -------------------------------------------------
def assignment_for(item, follower_gid: str):
    """Return (assignee_gid, propagation_recipient_name).
    use_existing/rename → the creator acts. propagation/triage → Robin
    decides (template owner unknown / no inferable team)."""
    if item.category in (TEMPLATE_PROPAGATION, TRIAGE):
        return follower_gid, "Robin"
    return (item.creator_gid or None), "Robin"


# entity type → section name in the cleanup project, and the parent-task title.
ENTITY_SECTION = {"field": "Custom Fields", "template": "Templates",
                  "portfolio": "Portfolios", "project": "Projects"}
ENTITY_BATCH_TITLE = {
    "field": "Custom fields to update to standard",
    "template": "Templates to review (rename or delete)",
    "portfolio": "Portfolios to review (rename or delete)",
    "project": "Projects to rename",
}
STALE_PROJECT_TITLE = "Clean up your stale Asana projects"


def fetch_sections(client: AsanaClient, project_gid: str) -> dict:
    """Return {section_name: section_gid} for the cleanup project (best-effort)."""
    try:
        secs = client.get(f"/projects/{project_gid}/sections", params={"opt_fields": "name"}).get("data", [])
        return {s.get("name"): s.get("gid") for s in secs}
    except Exception as e:
        print(f"  (could not fetch sections, tasks go to default: {e})")
        return {}


def priority_for(category: str, gov: dict):
    """PD-Priority value to stamp, or None to leave blank.
    'michelle' mode leaves it blank; 'auto' maps by issue impact."""
    if (gov.get("priority_mode") or "auto") != "auto":
        return None
    return (gov.get("priority_by_category") or {}).get(category)


_PRIORITY_RANK = {"Low": 1, "Medium": 2, "High": 3, "Urgent": 4}


def _max_priority(values):
    vals = [v for v in values if v]
    return max(vals, key=lambda v: _PRIORITY_RANK.get(v, 0)) if vals else None


def file_tasks(creator: TaskCreator, specs: list, gov: dict, task_results: dict) -> None:
    """Create cleanup tasks from specs. Items a person fixes themselves
    (assignee == creator) are bundled: >=batch_threshold of them → one parent
    task + a subtask each; otherwise a single task. Robin-assigned items
    (propagation/triage) are always individual."""
    from collections import defaultdict, OrderedDict
    title = gov.get("batch_task_title", "Update recent Asana adds to match standards")
    threshold = int(gov.get("batch_threshold", 2))

    # Collapse same (assignee, creator, title) specs — i.e. same-named clones —
    # into one item carrying all their GIDs, so a person never sees N identical
    # subtasks (e.g. two project-scoped "Timing" fields → one task).
    merged: "OrderedDict[tuple, dict]" = OrderedDict()
    for s in specs:
        key = (s["assignee_gid"], s["creator_gid"], s["title"])
        if key in merged:
            merged[key]["dedup_gids"].extend(s["dedup_gids"])
        else:
            merged[key] = {**s, "dedup_gids": list(s["dedup_gids"])}
    collapsed = list(merged.values())
    for s in collapsed:
        n = len(s["dedup_gids"])
        if n > 1:
            s["body"] += (f"\n\n(Heads up: {n} fields share this exact name on different "
                          f"projects — apply the same fix to each.)")

    def record(res, spec):
        for g in spec["dedup_gids"]:
            task_results[g] = res

    own, singles = defaultdict(list), []
    for s in collapsed:
        if s["assignee_gid"] and s["assignee_gid"] == s["creator_gid"]:
            # group per (person, batch title) so e.g. "recent adds" and "stale
            # projects" form separate parent tasks for the same person.
            own[(s["assignee_gid"], s.get("batch_title") or title)].append(s)
        else:
            singles.append(s)

    failures = 0
    for (assignee, batch_title), group in own.items():
        if len(group) >= threshold:
            try:
                res = creator.create_parent_with_subtasks(
                    assignee_gid=assignee, title=batch_title,
                    priority=_max_priority([g["priority"] for g in group]),
                    section_gid=group[0].get("section_gid"),
                    subtasks=[{"dedup_gids": g["dedup_gids"], "name": g["title"], "description": g["body"],
                               "entity_type": g.get("entity_type", "field")}
                              for g in group])
            except Exception as e:
                failures += 1
                print(f"  batch FAILED (skipped, will retry next run) for {assignee}: {e}")
                continue
            for g in group:
                record(res, g)
            print(f"  batch [{res['status']}] {res.get('title', title)} — {res.get('subtasks', 0)} subtasks (assignee {assignee})")
        else:
            singles.extend(group)

    for s in singles:
        try:
            res = creator.create(dedup_gids=s["dedup_gids"], title=s["title"], description=s["body"],
                                 assignee_gid=s["assignee_gid"], priority=s["priority"],
                                 entity_type=s.get("entity_type", "field"), section_gid=s.get("section_gid"))
        except Exception as e:
            failures += 1
            print(f"  task FAILED (skipped, will retry next run): {s['title']}: {e}")
            continue
        record(res, s)
        print(f"  task [{res['status']}] {s['title']}")
    if failures:
        print(f"  ({failures} task(s) failed and were skipped — idempotent, so a re-run picks them up.)")


TEMPLATE_STATUS = {
    TEMPLATE_COMPLIANT: "Approved / sanctioned",
    TEMPLATE_FORK: "⚠️ Possible fork",
    TEMPLATE_REFORMAT: "⚠️ Rename needed",
    TEMPLATE_TRIAGE: "Review",
}
PORTFOLIO_STATUS = {
    PORTFOLIO_COMPLIANT: "OK",
    PORTFOLIO_METADATA: "⚠️ Missing metadata",
    PORTFOLIO_REFORMAT: "⚠️ Rename needed",
}


def template_table_rows(all_items: list) -> tuple[list, list]:
    """(rows, flagged_lines) for the Notion TEMPLATES-LIVE table.
    Columns: Template name | Owner | Status."""
    rows, flagged = [], []
    for it in sorted(all_items, key=lambda x: x.name.lower()):
        status = TEMPLATE_STATUS.get(it.category, "")
        rows.append([it.name, it.owner_name, status])
        if it.category == TEMPLATE_FORK:
            flagged.append(f'"{it.name}" — similar to {it.similar_to}')
        elif it.category == TEMPLATE_REFORMAT:
            flagged.append(f'"{it.name}" — rename to {it.suggested_name}')
    return rows, flagged


def portfolio_table_rows(all_items: list) -> list:
    """Rows for the Notion PORTFOLIOS-LIVE table.
    Columns: Portfolio name | Owner | Scope | Status. (Sunset date / inclusion
    criteria omitted — no storage in Asana yet; revisit when fields exist.)"""
    rows = []
    for it in sorted(all_items, key=lambda x: x.name.lower()):
        rows.append([it.name.strip(), it.owner_name, it.scope or "—",
                     PORTFOLIO_STATUS.get(it.category, "")])
    return rows


def resolve_stale_tasks(creator: TaskCreator, field_checker, inventory: list) -> list:
    """Auto-complete open governance tasks whose underlying field is now compliant
    (renamed/prefixed) or deleted — so people don't get pinged for work already
    done, however it got done. v1 handles field tasks only; template/portfolio
    tasks are left alone. Mutates the task cache so resolved items don't also get
    flagged as aging. Returns the titles it closed."""
    by_gid = {str(r.get("gid")): r for r in inventory}
    resolved = []
    try:
        tasks = creator.fetch_project_tasks()
    except Exception as e:
        print(f"  (auto-resolve: could not read cleanup project: {e})")
        return resolved
    for t in tasks:
        if t.get("completed"):
            continue
        markers = t.get("governance_markers") or []
        if not markers or any(typ != "field" for typ, _ in markers):
            continue   # nothing to check, or has a non-field marker (skip in v1)

        def done(gid):
            row = by_gid.get(gid)
            return True if row is None else field_checker.is_compliant(row, inventory)

        if all(done(gid) for _typ, gid in markers):
            if not creator.dry_run:
                creator.complete_task(t["gid"], comment=(
                    "✅ Auto-resolved by governance: the field(s) this task covered now "
                    "match the standard (renamed, prefixed, or removed). Closing out — "
                    "no action needed."))
            t["completed"] = True   # keep the cache honest for the aging check
            resolved.append(t.get("name") or t.get("gid"))
    return resolved


def nudge_open_tasks(creator: TaskCreator, nudge_after_days: int) -> int:
    """Post a friendly nudge comment on still-open governance tasks older than
    nudge_after_days. Returns how many it nudged. Runs after auto-resolution, so
    tasks just closed this run aren't nudged."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    nudged = 0
    try:
        tasks = creator.fetch_project_tasks()
    except Exception:
        return 0
    for t in tasks:
        if t.get("completed") or not t.get("governance_markers"):
            continue
        cr = t.get("created_at")
        if not cr:
            continue
        try:
            age = (now - datetime.fromisoformat(cr.replace("Z", "+00:00"))).days
        except ValueError:
            continue
        if age < nudge_after_days:
            continue
        msg = (f"👋 Friendly nudge — this Asana clean-up task has been open {age} days. "
               f"A couple of minutes here keeps the workspace searchable for everyone. "
               f"If it's already handled, just mark it complete. Thank you!")
        if not creator.dry_run:
            creator.comment_task(t["gid"], msg)
        nudged += 1
    return nudged


def resolved_and_aging(creator: TaskCreator, baseline_date: str | None, aging_days: int):
    """Compute the 'Resolved this week' and 'Aging' lists from the cleanup
    project's governance tasks. Best-effort; returns ([], []) on any problem."""
    resolved, aging = [], []
    try:
        tasks = creator.fetch_project_tasks()
    except Exception as e:
        print(f"  (could not read cleanup project for resolved/aging: {e})")
        return resolved, aging
    now = datetime.now(timezone.utc)
    cutoff = None
    if baseline_date:
        try:
            cutoff = datetime.fromisoformat(baseline_date).replace(tzinfo=timezone.utc)
        except ValueError:
            cutoff = None
    for t in tasks:
        if not t.get("governance_id"):
            continue
        if t.get("completed"):
            ca = t.get("completed_at")
            if ca:
                done = datetime.fromisoformat(ca.replace("Z", "+00:00"))
                if cutoff is None or done >= cutoff:
                    resolved.append(f"{t.get('name')} ✓")
        else:
            cr = t.get("created_at")
            if cr:
                made = datetime.fromisoformat(cr.replace("Z", "+00:00"))
                age = (now - made).days
                if age > aging_days:
                    aging.append(f"{t.get('name')} — open {age} days")
    return resolved, aging


# --- main -------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Weekly Asana governance run "
                                             "(fields + templates + portfolios + projects).")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute everything; write nothing to Asana/Notion (local testing only)")
    ap.add_argument("--inventory", metavar="CSV",
                    help="use this field-inventory CSV instead of pulling fresh; also skips "
                         "the live entity pulls (local field testing)")
    ap.add_argument("--sweep", action="store_true",
                    help="one-time backfill: classify ALL current items (ignore the baseline) "
                         "to surface pre-existing drift. Idempotent. Skips Notion + baseline.")
    ap.add_argument("--sweep-target", default="fields",
                    help="comma list of what --sweep covers: fields,templates,portfolios,projects,all "
                         "(default fields). 'projects' = stale-project archive/rename sweep.")
    ap.add_argument("--creator", metavar="NAME",
                    help="with --sweep, restrict to fields created by this person "
                         '(exact display name, e.g. "Nina O\'Brien")')
    args = ap.parse_args()

    # Pacific date, not the runner's UTC date (an evening-PT run is already the
    # next day in UTC, which mislabeled the digest).
    try:
        from zoneinfo import ZoneInfo
        run_date = datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    except Exception:
        run_date = date.today().isoformat()
    ARTIFACTS.mkdir(exist_ok=True)
    mode = "DRY-RUN" if args.dry_run else "LIVE"
    print(f"=== Governance run {run_date} [{mode}] ===\n")

    standards = load_yaml("standards.yaml")
    creator_to_team = load_yaml("creator_to_team.yaml")
    gov = standards.get("governance") or {}

    # env / secrets
    asana_token = os.environ.get("ASANA_SERVICE_TOKEN")
    notion_token = os.environ.get("NOTION_TOKEN")
    notion_page = os.environ.get("NOTION_GOVERNANCE_PAGE_ID")
    cleanup_gid = os.environ.get("ASANA_CLEANUP_PROJECT_GID")
    slack_webhook = os.environ.get("SLACK_WEBHOOK_URL")

    reference_url = f"https://www.notion.so/{notion_page}" if notion_page else "the Notion governance doc"
    report_url = f"https://app.asana.com/0/{cleanup_gid}/list" if cleanup_gid else None

    # 1. inventory
    inv_path = args.inventory or (pull_inventory() if asana_token else None)
    if not inv_path:
        print("ERROR: no --inventory given and ASANA_SERVICE_TOKEN unset; nothing to do.")
        return 2
    inventory = read_inventory(inv_path)
    user_fields = [r for r in inventory if is_user_created(r)]
    print(f"Inventory: {inv_path}")
    print(f"  {len(inventory)} fields total · {len(user_fields)} user-created\n")

    # 2. select fields to classify
    baseline = load_baseline()
    field_base = baseline.get("field_gids") if baseline else None
    # which entity types this --sweep covers (default fields; "all" = everything)
    sweep_targets = set()
    if args.sweep:
        raw = (args.sweep_target or "fields").lower()
        sweep_targets = ({"fields", "templates", "portfolios", "projects"}
                         if "all" in raw else {t.strip() for t in raw.split(",") if t.strip()})

    if args.sweep and "fields" in sweep_targets:
        # One-time backfill: ignore the baseline, classify everything (optionally
        # scoped to one creator). Idempotent dedup stops re-filing on weekly runs.
        new_fields = user_fields
        if args.creator:
            new_fields = [r for r in new_fields if (r.get("created_by") or "") == args.creator]
        print(f"SWEEP fields: classifying {len(new_fields)} existing field(s)"
              + (f" by {args.creator}" if args.creator else "") + " (baseline ignored).\n")
    elif args.sweep:
        new_fields = []   # sweeping other targets, not fields
    elif not field_base:
        print("No field baseline → establishing it. No field tasks this run.\n")
        new_fields = []
    else:
        new_fields = [r for r in user_fields if str(r.get("gid")) not in field_base]
        print(f"Diff vs baseline: {len(new_fields)} new user-created field(s).\n")

    # 3. classify
    checker = FieldChecker(standards, creator_to_team)
    items = checker.classify(new_fields, inventory)
    if items:
        print("Suggestions:")
        for it in items:
            tail = it.standard or it.suggested_name or (f"cluster x{it.cluster_count}" if it.cluster_count else "")
            print(f"  [{it.category}] {it.field_name} → {tail}")
        print()

    # 4. build task specs (fields)
    task_results: dict = {}
    follower_gid = os.environ.get("GOVERNANCE_FOLLOWER_GID", "1434924069037136")
    can_touch_asana = bool(asana_token and cleanup_gid)
    sections = fetch_sections(AsanaClient(asana_token), cleanup_gid) if can_touch_asana else {}

    def section_for(entity_type: str):
        return sections.get(ENTITY_SECTION.get(entity_type, ""))

    specs = []
    for it in items:
        assignee, recipient = assignment_for(it, follower_gid)
        title, body = task_templates.build(it, reference_url, propagation_recipient_name=recipient)
        specs.append({"dedup_gids": [it.field_gid], "assignee_gid": assignee,
                      "creator_gid": str(it.creator_gid or ""), "priority": priority_for(it.category, gov),
                      "title": title, "body": body, "entity_type": "field",
                      "batch_title": ENTITY_BATCH_TITLE["field"], "section_gid": section_for("field")})

    # 4b. templates + portfolios + projects.
    # Normal run: NEW-ONLY (diff vs per-entity baseline); template/portfolio
    # classify_all also feeds the Notion tables.
    # Sweep run: classify ALL of the targeted entities (ignore baseline); projects
    # use a staleness sweep (archive/rename), not naming-new-only.
    template_items, portfolio_items, project_items = [], [], []
    template_all, portfolio_all = [], []
    cur_template_gids = cur_portfolio_gids = cur_project_gids = None
    sweeping_entities = bool(sweep_targets & {"templates", "portfolios", "projects"})
    do_entities = asana_token and not args.inventory and (not args.sweep or sweeping_entities)
    if do_entities:
        ent_client = AsanaClient(asana_token)
        tmpl_base = (baseline or {}).get("template_gids")
        port_base = (baseline or {}).get("portfolio_gids")
        proj_base = (baseline or {}).get("project_gids")

        def _new_only(actionable, base):
            return [] if not base else [it for it in actionable if it.gid not in base]

        if not args.sweep or "templates" in sweep_targets:
            try:
                templates = pull_entities.pull_templates(ent_client)
                cur_template_gids = {t["gid"] for t in templates}
                template_all = TemplateChecker(standards, creator_to_team).classify_all(templates)
                actionable = [it for it in template_all if it.category != TEMPLATE_COMPLIANT]
                template_items = actionable if args.sweep else _new_only(actionable, tmpl_base)
                print(f"  templates: {len(templates)} total · {len(template_items)} flagged"
                      + (" (sweep: all)" if args.sweep else " (new)"))
            except Exception as e:
                print(f"  template pull/classify failed (non-fatal): {e}")
        if not args.sweep or "portfolios" in sweep_targets:
            try:
                portfolios = pull_entities.pull_portfolios(ent_client)
                cur_portfolio_gids = {p["gid"] for p in portfolios}
                portfolio_all = PortfolioChecker(standards, creator_to_team).classify_all(portfolios)
                actionable = [it for it in portfolio_all if it.category != PORTFOLIO_COMPLIANT]
                portfolio_items = actionable if args.sweep else _new_only(actionable, port_base)
                print(f"  portfolios: {len(portfolios)} total · {len(portfolio_items)} flagged"
                      + (" (sweep: all)" if args.sweep else " (new)"))
            except Exception as e:
                print(f"  portfolio pull/classify failed (non-fatal): {e}")

        # projects: stale sweep (one-time) OR new-only naming (weekly)
        stale_specs = []
        if args.sweep and "projects" in sweep_targets:
            try:
                projects = pull_entities.pull_projects(ent_client)
                cur_project_gids = {p["gid"] for p in projects}
                stale_days = int(gov.get("stale_project_days", 180))
                cutoff = datetime.now(timezone.utc) - timedelta(days=stale_days)
                pchecker = ProjectChecker(standards, creator_to_team)
                stale = []
                for p in projects:
                    ma = p.get("modified_at")
                    if not ma:
                        continue
                    try:
                        when = datetime.fromisoformat(ma.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if when < cutoff:
                        stale.append((p, (datetime.now(timezone.utc) - when).days))
                for p, age in stale:
                    pi = pchecker._classify_one(p)
                    suggested = pi.suggested_name if pi.category != "project_compliant" else None
                    title, body = task_templates.project_cleanup(
                        name=p["name"], gid=p["gid"], owner_name=(p.get("owner") or {}).get("name", ""),
                        stale_days=age, suggested_name=suggested, issues=pi.issues, reference_url=reference_url)
                    owner_gid = str((p.get("owner") or {}).get("gid") or "")
                    stale_specs.append({"dedup_gids": [p["gid"]], "assignee_gid": (owner_gid or follower_gid),
                                        "creator_gid": (owner_gid or follower_gid), "priority": "Low",
                                        "title": title, "body": body, "entity_type": "project",
                                        "batch_title": STALE_PROJECT_TITLE, "section_gid": section_for("project")})
                print(f"  SWEEP projects: {len(projects)} total · {len(stale)} stale (> {stale_days}d)")
            except Exception as e:
                print(f"  stale-project sweep failed (non-fatal): {e}")
        elif not args.sweep:
            try:
                projects = pull_entities.pull_projects(ent_client)
                cur_project_gids = {p["gid"] for p in projects}
                if not proj_base:
                    print(f"  projects: {len(projects)} total · establishing baseline (no tasks)")
                else:
                    proj_new = [p for p in projects if p["gid"] not in proj_base]
                    project_items = ProjectChecker(standards, creator_to_team).classify(proj_new)
                    print(f"  projects: {len(projects)} total · {len(proj_new)} new · {len(project_items)} misnamed")
            except Exception as e:
                print(f"  project pull/classify failed (non-fatal): {e}")

        # Owner-less items (no owner set in Asana) go to Robin, not orphaned.
        for it in template_items:
            title, body = task_templates.build_template(it, reference_url)
            specs.append({"dedup_gids": [it.gid], "assignee_gid": (it.owner_gid or follower_gid),
                          "creator_gid": str(it.owner_gid or ""), "priority": priority_for(it.category, gov),
                          "title": title, "body": body, "entity_type": "template",
                          "batch_title": ENTITY_BATCH_TITLE["template"], "section_gid": section_for("template")})
        for it in portfolio_items:
            title, body = task_templates.build_portfolio(it, reference_url)
            specs.append({"dedup_gids": [it.gid], "assignee_gid": (it.owner_gid or follower_gid),
                          "creator_gid": str(it.owner_gid or ""), "priority": priority_for(it.category, gov),
                          "title": title, "body": body, "entity_type": "portfolio",
                          "batch_title": ENTITY_BATCH_TITLE["portfolio"], "section_gid": section_for("portfolio")})
        for it in project_items:
            title, body = task_templates.build_project(it, reference_url)
            specs.append({"dedup_gids": [it.gid], "assignee_gid": (it.owner_gid or follower_gid),
                          "creator_gid": str(it.owner_gid or ""), "priority": priority_for(it.category, gov),
                          "title": title, "body": body, "entity_type": "project",
                          "batch_title": ENTITY_BATCH_TITLE["project"], "section_gid": section_for("project")})
        specs.extend(stale_specs)

    # 4c. file all specs (self-fix items bundled per person)
    creator = None
    if specs and can_touch_asana:
        creator = TaskCreator(AsanaClient(asana_token), cleanup_gid, dry_run=args.dry_run,
                              follower_gid=follower_gid, due_days=int(gov.get("task_due_days", 7)))
        file_tasks(creator, specs, gov, task_results)
        print()
    elif specs and not can_touch_asana:
        print("  (skipping task creation — ASANA_SERVICE_TOKEN / ASANA_CLEANUP_PROJECT_GID not set)\n")

    # auto-resolve stale tasks + compute resolved / aging (needs a creator)
    resolved, aging = [], []
    if can_touch_asana:
        if creator is None:
            creator = TaskCreator(AsanaClient(asana_token), cleanup_gid, dry_run=args.dry_run,
                                  follower_gid=follower_gid)
        auto_resolved = resolve_stale_tasks(creator, checker, inventory)
        for title in auto_resolved:
            print(f"  auto-resolved{' (dry-run)' if args.dry_run else ''}: {title}")
        nudged = nudge_open_tasks(creator, int(gov.get("nudge_after_days", 14)))
        if nudged:
            print(f"  nudged {nudged} open task(s){' (dry-run)' if args.dry_run else ''}")
        bdate = baseline.get("run_date") if baseline else None
        resolved_manual, aging = resolved_and_aging(creator, bdate, int(gov.get("aging_days", 30)))
        resolved = [f"{t} ✓ (auto-resolved)" for t in auto_resolved] + resolved_manual

    # 5. Notion sync
    if notion_token and notion_page and not args.dry_run and not args.sweep:
        nclient = NotionClient(notion_token)
        try:
            print(f"Notion PD-SET sync: {sync_pd_set(nclient, notion_page, inventory, run_date)}")
        except Exception as e:
            print(f"Notion PD-SET sync FAILED (non-fatal): {e}")
        try:
            team_prefixes = list(standards["field_prefixes"]["teams"].keys())
            print(f"Notion TEAM-LIBRARIES sync: "
                  f"{sync_team_libraries(nclient, notion_page, inventory, team_prefixes, run_date)}")
        except Exception as e:
            print(f"Notion TEAM-LIBRARIES sync FAILED (non-fatal): {e}")
        # Templates/portfolios are intentionally NOT listed in Notion — they're
        # managed via Asana cleanup tasks. The doc is a custom-field search tool only.
        print()
    else:
        print("Notion sync: skipped (dry-run or missing NOTION_TOKEN/PAGE_ID).\n")

    # 6. Slack
    message = compose.build_message(run_date=run_date, items=items, task_results=task_results,
                                    resolved=resolved, aging=aging, report_url=report_url,
                                    template_items=template_items, portfolio_items=portfolio_items,
                                    project_items=project_items)
    slack_out = ARTIFACTS / f"slack_message_{run_date}.json"
    slack_out.write_text(json.dumps(message, indent=2), encoding="utf-8")
    if slack_webhook and not args.dry_run:
        try:
            compose.post(slack_webhook, message)
            print("Slack digest: posted.\n")
        except Exception as e:
            print(f"Slack post FAILED (non-fatal): {e}\n  (message saved to {slack_out})\n")
    else:
        print(f"Slack digest: not posted ({'dry-run' if args.dry_run else 'no webhook'}); "
              f"saved to {slack_out}\n")

    # 7. baseline + artifact
    if args.sweep:
        print("Baseline: not written (sweep — leaves the weekly cadence untouched).")
    elif not args.dry_run:
        write_baseline(baseline, run_date,
                       field_gids={str(r.get("gid")) for r in inventory if r.get("gid")},
                       template_gids=cur_template_gids, portfolio_gids=cur_portfolio_gids,
                       project_gids=cur_project_gids)
        print(f"Baseline updated: {BASELINE_PATH}")
    else:
        print("Baseline: not written (dry-run).")
    # keep a copy of the pull for the run record
    try:
        (ARTIFACTS / Path(inv_path).name).write_bytes(Path(inv_path).read_bytes())
    except Exception:
        pass

    print(f"\n=== Done [{mode}] ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
