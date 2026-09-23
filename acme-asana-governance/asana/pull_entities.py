"""
asana/pull_entities.py — pull project templates and portfolios for Sprint 2.

Returns data already normalised for checkers/templates.py and
checkers/portfolios.py. Uses the shared AsanaClient (retry/backoff).

API notes (verified against live objects 2026-06-18):
  * Project templates: GET /project_templates?workspace=... → name, owner, team,
    description. (Templates carry a real `description`; portfolios do not.)
  * Portfolios: there is NO workspace-wide list endpoint — GET /portfolios
    requires both `workspace` AND `owner`. So we iterate workspace users and
    union the results, deduping by gid. Membership comes from
    GET /portfolios/{gid}/items (item_count).
"""

from __future__ import annotations

from asana.task_creator import AsanaClient

WORKSPACE_GID = os.environ.get("ASANA_WORKSPACE_GID", "")  # set in env / Actions secrets


def _paginate(client: AsanaClient, path: str, params: dict) -> list:
    out, params = [], dict(params)
    params.setdefault("limit", 100)
    while True:
        resp = client.get(path, params=params)
        out.extend(resp.get("data", []))
        nxt = resp.get("next_page") or {}
        if nxt.get("offset"):
            params["offset"] = nxt["offset"]
        else:
            break
    return out


def pull_templates(client: AsanaClient, workspace: str = WORKSPACE_GID) -> list:
    """Normalised list: {gid, name, owner_name, owner_gid, team_name, description}.

    PD's workspace is an Organization, and GET /project_templates rejects a
    workspace param for orgs — templates must be listed per team. So we iterate
    the org's teams and union their templates (deduped by gid)."""
    teams = _paginate(client, f"/organizations/{workspace}/teams", {"opt_fields": "name"})
    out, seen = [], set()
    for team in teams:
        try:
            raw = _paginate(client, f"/teams/{team['gid']}/project_templates", {
                "opt_fields": "name,owner.name,owner.gid,team.name,description",
            })
        except Exception:
            continue   # team with no templates / no access
        for t in raw:
            gid = t.get("gid")
            if not gid or gid in seen:
                continue
            seen.add(gid)
            owner = t.get("owner") or {}
            out.append({
                "gid": gid,
                "name": t.get("name") or "",
                "owner_name": owner.get("name") or "",
                "owner_gid": owner.get("gid") or "",
                "team_name": (t.get("team") or {}).get("name") or "",
                "description": t.get("description") or "",
            })
    return out


def pull_projects(client: AsanaClient, workspace: str = WORKSPACE_GID) -> list:
    """Normalised list of ACTIVE projects: {gid, name, owner{gid,name}, team_name}.
    One paginated list call (cheap) — owner/team drive TEAM inference. Archived
    projects are excluded (we only govern active naming)."""
    raw = _paginate(client, "/projects", {
        "workspace": workspace,
        "archived": "false",
        "opt_fields": "name,archived,owner.name,owner.gid,team.name,modified_at",
    })
    out = []
    for p in raw:
        if p.get("archived"):
            continue
        owner = p.get("owner") or {}
        out.append({
            "gid": p.get("gid"),
            "name": p.get("name") or "",
            "owner": {"gid": owner.get("gid") or "", "name": owner.get("name") or ""},
            "team_name": (p.get("team") or {}).get("name") or "",
            "modified_at": p.get("modified_at") or "",
        })
    return out


def pull_portfolios(client: AsanaClient, workspace: str = WORKSPACE_GID) -> list:
    """Normalised list ready for PortfolioChecker. Iterates users because Asana
    has no workspace-wide portfolio list. Each: {gid, name, owner{gid,name},
    item_count, custom_fields:[{name,display_value}]}."""
    users = _paginate(client, "/users", {"workspace": workspace, "opt_fields": "name"})

    seen: dict[str, dict] = {}
    for u in users:
        try:
            owned = _paginate(client, "/portfolios", {
                "workspace": workspace,
                "owner": u.get("gid"),
                "opt_fields": "name,owner.name,owner.gid",
            })
        except Exception:
            continue   # a user with no portfolio access can 4xx; skip
        for p in owned:
            if p.get("gid") not in seen:
                seen[p["gid"]] = p

    portfolios = []
    for gid, p in seen.items():
        # membership (item count) + any portfolio-level custom field values
        try:
            items = _paginate(client, f"/portfolios/{gid}/items", {"opt_fields": "name"})
            item_count = len(items)
        except Exception:
            item_count = 0
        try:
            detail = client.get(f"/portfolios/{gid}", {
                "opt_fields": "name,owner.name,owner.gid,custom_fields.name,custom_fields.display_value",
            }).get("data", {})
        except Exception:
            detail = p
        owner = detail.get("owner") or {}
        portfolios.append({
            "gid": gid,
            "name": detail.get("name") or p.get("name") or "",
            "owner": {"gid": owner.get("gid") or "", "name": owner.get("name") or ""},
            "item_count": item_count,
            "custom_fields": detail.get("custom_fields") or [],
        })
    return portfolios
