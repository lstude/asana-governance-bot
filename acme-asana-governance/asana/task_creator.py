"""
asana/task_creator.py — creates governance cleanup tasks in Asana.

Design points that make this safe to run unattended every week:

  * Idempotent. Every task carries a hidden marker line `governance-id:<gid>`
    in its notes. Before creating, we read the cleanup project's existing tasks
    (open + recently completed) and skip any field that already has one. Safe to
    re-run the same week — it creates nothing the second time.

  * Pre-assigned. The task is assigned to the field's creator (their Asana GID
    comes straight from the inventory), with the governance follower (Robin)
    added so she's notified on completion. No human has to assign anything.

  * Retry/backoff. The project custom_field_settings endpoint has been flaky;
    every GET/POST is wrapped in exponential backoff on 429/5xx/network errors.

  * Dry-run. `dry_run=True` (set via --dry-run locally) logs what *would* be
    created and writes nothing. Production CI runs live (dry_run=False).
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, timedelta
from urllib import request, error, parse

BASE = "https://app.asana.com/api/1.0"

# Governance follower — Robin Vance. Overridable via env for portability.
DEFAULT_FOLLOWER_GID = os.environ.get("GOVERNANCE_FOLLOWER_GID", "1434924069037136")

GOVERNANCE_MARKER = "governance-id:"   # hidden dedup token written into task notes


def _marker(entity_type: str, gid: str) -> str:
    """A dedup/resolution marker line: governance-id:<type>:<gid>."""
    return f"{GOVERNANCE_MARKER}{entity_type}:{gid}"


def _parse_markers(notes: str) -> list:
    """Return [(entity_type, gid), ...] from a task's notes.
    Legacy lines `governance-id:<gid>` (no type) are read as ('field', gid)."""
    out = []
    for line in (notes or "").splitlines():
        line = line.strip()
        if not line.startswith(GOVERNANCE_MARKER):
            continue
        rest = line[len(GOVERNANCE_MARKER):].strip()
        if ":" in rest:
            typ, gid = rest.split(":", 1)
            out.append((typ.strip(), gid.strip()))
        else:
            out.append(("field", rest))
    return out


class AsanaError(RuntimeError):
    pass


class AsanaClient:
    def __init__(self, token: str, max_retries: int = 8, base_delay: float = 1.0):
        self.token = token
        self.max_retries = max_retries
        self.base_delay = base_delay

    def _request(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        qs = ("?" + parse.urlencode(params)) if params else ""
        url = f"{BASE}{path}{qs}"
        data = json.dumps({"data": body}).encode("utf-8") if body is not None else None
        headers = {
            "Authorization": f"Bearer {self.token}",
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
                # Retry on rate-limit and transient server errors; fail fast otherwise.
                if code == 429 or 500 <= code < 600:
                    retry_after = e.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after else self.base_delay * (2 ** attempt)
                    last_err = AsanaError(f"{method} {path} -> HTTP {code}")
                    time.sleep(delay)
                    continue
                detail = e.read().decode("utf-8", errors="replace")
                raise AsanaError(f"{method} {path} -> HTTP {code}: {detail}") from e
            except Exception as e:  # network / timeout
                last_err = e
                time.sleep(self.base_delay * (2 ** attempt))
        raise AsanaError(f"{method} {path} failed after {self.max_retries} attempts: {last_err}")

    def get(self, path, params=None):
        return self._request("GET", path, params=params)

    def post(self, path, body):
        return self._request("POST", path, body=body)

    def put(self, path, body):
        return self._request("PUT", path, body=body)


class TaskCreator:
    def __init__(self, client: AsanaClient, project_gid: str, dry_run: bool = False,
                 follower_gid: str = DEFAULT_FOLLOWER_GID, due_days: int = 7):
        self.client = client
        self.project_gid = project_gid
        self.dry_run = dry_run
        self.follower_gid = follower_gid
        self.due_days = due_days
        self._cf_cache: dict | None = None
        self._existing_keys: set | None = None
        self._tasks_cache: list | None = None

    # -- cleanup-project custom fields (PD-Priority) --------------------------
    def _resolve_custom_fields(self) -> dict:
        """Map PD-Priority to its GID + option GIDs from the cleanup project's
        settings. Cached for the run. (PD-Owner/DRI is no longer stamped — tasks
        are auto-assigned to the creator, so a DRI field is redundant.)"""
        if self._cf_cache is not None:
            return self._cf_cache
        opt = "custom_field.gid,custom_field.name,custom_field.enum_options.gid,custom_field.enum_options.name"
        resp = self.client.get(
            f"/projects/{self.project_gid}/custom_field_settings",
            params={"opt_fields": opt},
        )
        cf = {"priority_gid": None, "priority_options": {}}
        for setting in resp.get("data", []):
            field = setting.get("custom_field") or {}
            if (field.get("name") or "").strip() == "PD-Priority":
                cf["priority_gid"] = field.get("gid")
                cf["priority_options"] = {
                    (o.get("name") or "").strip(): o.get("gid")
                    for o in (field.get("enum_options") or [])
                }
        self._cf_cache = cf
        return cf

    def _build_custom_fields_payload(self, priority: str | None) -> dict:
        """Stamp PD-Priority to the given value, if a value is supplied and the
        field+option exist. priority=None → leave it blank (Robin sets it)."""
        if not priority:
            return {}
        cf = self._resolve_custom_fields()
        opt_gid = cf["priority_options"].get(priority) if cf["priority_gid"] else None
        return {cf["priority_gid"]: opt_gid} if opt_gid else {}

    # -- project tasks (shared by dedup, resolved, aging) ---------------------
    def fetch_project_tasks(self) -> list:
        """All tasks in the cleanup project, with the fields needed for dedup,
        resolved-this-week and aging. Cached for the run. Each returned dict adds
        a parsed `governance_id` (None if the task wasn't filed by this system)."""
        if self._tasks_cache is not None:
            return self._tasks_cache
        tasks = []
        params = {"opt_fields": "name,notes,completed,completed_at,created_at,permalink_url", "limit": 100}
        path = f"/projects/{self.project_gid}/tasks"
        while True:
            resp = self.client.get(path, params=params)
            for t in resp.get("data", []):
                # ALL markers — a grouped parent lists one per subtask.
                t["governance_markers"] = _parse_markers(t.get("notes") or "")
                tasks.append(t)
            nxt = resp.get("next_page") or {}
            if nxt.get("offset"):
                params["offset"] = nxt["offset"]
            else:
                break
        self._tasks_cache = tasks
        return tasks

    def _load_existing_keys(self) -> set:
        """All GIDs that already have a governance task (open or done) — across
        single tasks and grouped parents (which list one marker per subtask)."""
        if self._existing_keys is not None:
            return self._existing_keys
        keys = set()
        for t in self.fetch_project_tasks():
            for _typ, gid in t.get("governance_markers", []):
                keys.add(gid)
        self._existing_keys = keys
        return keys

    def complete_task(self, task_gid: str, comment: str | None = None) -> None:
        """Mark a task complete, optionally leaving a comment first."""
        if comment:
            self.client.post(f"/tasks/{task_gid}/stories", body={"text": comment})
        self.client.put(f"/tasks/{task_gid}", body={"completed": True})

    def comment_task(self, task_gid: str, text: str) -> None:
        """Post a comment (story) on a task — used for friendly nudges."""
        self.client.post(f"/tasks/{task_gid}/stories", body={"text": text})

    # -- create ---------------------------------------------------------------
    def _add_to_section(self, task_gid: str, section_gid: str | None) -> None:
        """Move a task into a section (Asana doesn't accept section on create)."""
        if task_gid and section_gid:
            try:
                self.client.post(f"/sections/{section_gid}/addTask", {"task": task_gid})
            except Exception as e:
                print(f"    (could not move {task_gid} to section: {e})")

    def create(self, *, dedup_gids: list, title: str, description: str,
               assignee_gid: str | None, priority: str | None = None,
               entity_type: str = "field", section_gid: str | None = None) -> dict:
        """
        Create one cleanup task covering one or more source GIDs (multiple when
        same-named clones were collapsed). Skips if all GIDs already have a task.
        priority: PD-Priority value to stamp, or None to leave blank.
        entity_type: "field" | "template" | "portfolio" | "project".
        Returns {"status": "created"|"skipped"|"dry-run", ...}.
        """
        existing = self._load_existing_keys()
        if all(g in existing for g in dedup_gids):
            return {"status": "skipped", "reason": "already exists", "dedup_gids": dedup_gids, "title": title}

        notes = f"{description}\n\n—\n" + "\n".join(_marker(entity_type, g) for g in dedup_gids)
        due_on = (date.today() + timedelta(days=self.due_days)).isoformat()

        body = {
            "name": title,
            "notes": notes,
            "due_on": due_on,
            "followers": [self.follower_gid],
            "projects": [self.project_gid],
        }
        if assignee_gid:
            body["assignee"] = assignee_gid
        # Resolve PD-Priority even in dry-run so config problems surface
        # before going live; we just don't POST in dry-run.
        cf_payload = self._build_custom_fields_payload(priority)
        if cf_payload:
            body["custom_fields"] = cf_payload

        if self.dry_run:
            for g in dedup_gids:
                self._existing_keys.add(g)
            return {"status": "dry-run", "dedup_gids": dedup_gids, "title": title,
                    "assignee": assignee_gid, "due_on": due_on, "custom_fields": cf_payload}

        resp = self.client.post("/tasks", body=body)
        created = resp.get("data", {})
        self._add_to_section(created.get("gid"), section_gid)
        # record so a later create() in the same run won't duplicate
        for g in dedup_gids:
            self._existing_keys.add(g)
        return {"status": "created", "dedup_gids": dedup_gids, "title": title,
                "task_gid": created.get("gid"), "permalink": created.get("permalink_url")}

    def create_parent_with_subtasks(self, *, assignee_gid: str | None, title: str,
                                    priority: str | None, subtasks: list,
                                    section_gid: str | None = None) -> dict:
        """
        One parent task assigned to `assignee_gid`, with one subtask per item so a
        person who created many fields gets a single thing in My Tasks, not N.
        subtasks: list of {dedup_gids: [...], name, description, entity_type}.
        (dedup_gids is a list so same-named clones collapse into one subtask.)

        Dedup: every GID is recorded in the PARENT's notes (the dedup scan reads
        project-level tasks, so parent notes are what's checked). Subtasks carry
        no project + no marker, so they don't re-flag.
        """
        existing = self._load_existing_keys()
        new_subs = [s for s in subtasks if not all(g in existing for g in s["dedup_gids"])]
        if not new_subs:
            return {"status": "skipped", "reason": "all already exist", "title": title}

        all_ids = [g for s in new_subs for g in s["dedup_gids"]]
        parent_notes = ("Batch cleanup — work each subtask below to bring your recent custom "
                        "fields in line with the Asana naming standards. Check them off as you go.\n"
                        "\n—\n" + "\n".join(_marker(s.get("entity_type", "field"), g)
                                            for s in new_subs for g in s["dedup_gids"]))
        due_on = (date.today() + timedelta(days=self.due_days)).isoformat()
        body = {"name": f"{title} ({len(new_subs)})", "notes": parent_notes,
                "due_on": due_on, "followers": [self.follower_gid],
                "projects": [self.project_gid]}
        if assignee_gid:
            body["assignee"] = assignee_gid
        cf_payload = self._build_custom_fields_payload(priority)
        if cf_payload:
            body["custom_fields"] = cf_payload

        if self.dry_run:
            for g in all_ids:
                self._existing_keys.add(g)
            return {"status": "dry-run", "title": body["name"], "assignee": assignee_gid,
                    "subtasks": len(new_subs), "dedup_gids": all_ids}

        parent = self.client.post("/tasks", body=body).get("data", {})
        pgid = parent.get("gid")
        self._add_to_section(pgid, section_gid)
        for s in new_subs:
            # subtask: parent only (no project), so it lives under the parent and
            # doesn't appear as its own top-level task in the cleanup project.
            self.client.post("/tasks", body={"name": s["name"], "notes": s["description"], "parent": pgid})
        for g in all_ids:
            self._existing_keys.add(g)
        return {"status": "created", "title": body["name"], "task_gid": pgid,
                "permalink": parent.get("permalink_url"), "subtasks": len(new_subs), "dedup_gids": all_ids}
