"""
asana/task_templates.py — plain-English task descriptions, one per field
category. Prose is taken verbatim from the spec; only the specifics (name,
standard, counts, team, reference link) are interpolated.

Each builder returns (title, description). The orchestrator passes the result
straight to task_creator.create_task().

Sprint 1 covers the three field categories. Project / portfolio / template
templates land in later sprints.
"""

from __future__ import annotations

from checkers.fields import (
    FieldItem,
    USE_EXISTING,
    RENAME,
    TEMPLATE_PROPAGATION,
    TRIAGE,
)


def _first_name(full_name: str) -> str:
    return (full_name or "there").strip().split()[0] if (full_name or "").strip() else "there"


def _portfolio_url(gid: str) -> str:
    return f"https://app.asana.com/0/portfolio/{gid}/list"


def _template_url(gid: str) -> str:
    return f"https://app.asana.com/0/project-templates/{gid}/list"


def _project_url(gid: str) -> str:
    return f"https://app.asana.com/0/{gid}/list"


def _host_link_line(item) -> str:
    """A direct link to the project that hosts a project-scoped field, if known."""
    gid = getattr(item, "host_project_gid", "") or ""
    name = getattr(item, "host_project_name", "") or "the project where you added it"
    if gid:
        return f"\n\nYour field is here: {name} → {_project_url(gid)}"
    return ""


def _field_scope_note(item) -> str:
    """Findability help (used where the action is 'remove/replace this field')."""
    if (getattr(item, "scope", "") or "") == "project-scoped":
        return ("\n\nWhere to find it: this is a PROJECT-SCOPED field, so it does NOT appear in "
                "the global Custom Field Library." + _host_link_line(item) +
                '\nOpen that project and click the field\'s column header (or "+ Add field" area) '
                "to edit it there.")
    return ("\n\nWhere to find it: Customize ▸ Fields on any project that uses it, or the global "
            "Custom Field Library (gear ▸ More ▸ Fields & templates) if you're an admin.")


def _field_rename_scope_note(item) -> str:
    """For rename tasks: decide scope (global preferred), then rename either way."""
    if (getattr(item, "scope", "") or "") == "project-scoped":
        return (
            "\n\nThis is currently a PROJECT-SCOPED field (it lives only on the project where you "
            "created it, so it isn't in the global Custom Field Library)." + _host_link_line(item) +
            "\n\nFirst, decide its scope: our objective is to primarily use GLOBAL custom fields, to "
            "improve org-wide visibility and reporting. But if this is truly a one-off you'll only "
            "use once or twice, it's fine to keep it scoped to the project. Either way, please "
            "update the name to match our standards.\n\n"
            "How: open that project ▸ click the field's column header ▸ Edit field ▸ rename it. In "
            "that same dialog you can choose to add it to the workspace library (make it global) if "
            "you'd like it available org-wide.")
    return ("\n\nHow: rename it in Customize ▸ Fields on any project that uses it, or in the global "
            "Custom Field Library (gear ▸ More ▸ Fields & templates) if you're an admin.")


def _projects_phrase(n: int | None) -> str:
    if not n:
        return "multiple projects"
    return f"{n} project" + ("s" if n != 1 else "")


def use_existing(item: FieldItem, reference_url: str) -> tuple[str, str]:
    who = _first_name(item.creator_name)
    standard = item.standard
    proj = _projects_phrase(item.standard_project_count)
    title = f'Replace your "{item.field_name}" field with {standard}'
    body = f"""Hi {who},

Saw you created a "{item.field_name}" custom field. Heads up: we already have {standard} as a workspace-wide standard — it's on {proj}.

What to do:
1. Open the project where you added "{item.field_name}"
2. Remove that field
3. Add {standard} instead (Customize ▸ Field gallery ▸ search "{standard}")
{_field_scope_note(item)}

Reference (standards + how-to): {reference_url}

Why this matters: when everyone makes their own version of this field, none of them connect to each other, none roll up to portfolios, and reporting becomes impossible. Using the workspace standard fixes all of that.

Thanks!"""
    return title, body


def rename(item: FieldItem, reference_url: str) -> tuple[str, str]:
    who = _first_name(item.creator_name)
    title = f'Rename custom field "{item.field_name}" to "{item.suggested_name}"'
    body = f"""Hi {who},

You created a custom field called "{item.field_name}". To keep the library scannable, we prefix team-specific fields so they cluster together.

What to do: rename "{item.field_name}" to "{item.suggested_name}".
{_field_rename_scope_note(item)}

Reference (standards + how-to): {reference_url}

Why this matters: with 100+ fields in the library, alphabetical clustering by team (PD-, BIZOPS-, MKTG-, etc.) is the only way to find anything.

Thanks!"""
    return title, body


def template_propagation(item: FieldItem, reference_url: str, recipient_name: str = "Robin") -> tuple[str, str]:
    who = _first_name(recipient_name)
    n = item.cluster_count or 0
    title = f'Template propagation: {n} new "{item.field_name}" copies this week'
    body = f"""Hi {who},

We've detected template-driven duplication: {n} new "{item.field_name}" custom fields appeared this week, all project-scoped, created by {item.creator_name}. This pattern almost always means a template still carries a local "{item.field_name}" field instead of a shared global one.

Diagnosis: when a template has a local field, every clone copies it as a fresh project-scoped field (separate field, separate GID, separate data — even though the name is the same). That's how you get {n} identical-looking "{item.field_name}" fields in one week.

What to do:
1. Find the template these projects were cloned from (start with {item.creator_name}'s recent projects)
2. Open that template
3. Remove the local "{item.field_name}" field
4. Add the shared global field instead (Customize → Field gallery)
5. Save

Going forward, all new projects from that template will reference the same global field — one field, all data rolls up.

(The {n} existing project-scoped copies will remain on their projects until those projects archive. That's fine.)

Reference: {reference_url}

Thanks!"""
    return title, body


def triage(item: FieldItem, reference_url: str, recipient_name: str = "Robin") -> tuple[str, str]:
    who = _first_name(recipient_name)
    scope_note = "project-scoped, may be fine" if item.scope == "project-scoped" else "global"
    title = f'Triage custom field "{item.field_name}" (by {item.creator_name})'
    body = f"""Hi {who},

A new custom field needs a human decision — it doesn't match an existing standard and we couldn't infer the right team prefix automatically.

Field: "{item.field_name}"
Created by: {item.creator_name}
Scope: {scope_note}

What to do: decide one of —
- It duplicates an existing standard → ask {_first_name(item.creator_name)} to swap to that standard.
- It's a legitimate team field → rename with the right team prefix ([TEAM]-{item.field_name}).
- It should be workspace-wide → route through the Custom Field Request Form.
- It's a genuine one-off → leave it and mark this task complete.

Reference: {reference_url}

Thanks!"""
    return title, body


# Dispatch table so the orchestrator can resolve category → builder.
BUILDERS = {
    USE_EXISTING: use_existing,
    RENAME: rename,
    TEMPLATE_PROPAGATION: template_propagation,
    TRIAGE: triage,
}


def build(item: FieldItem, reference_url: str, propagation_recipient_name: str = "Robin") -> tuple[str, str]:
    """Resolve and render the right field template for an item."""
    if item.category in (TEMPLATE_PROPAGATION, TRIAGE):
        return BUILDERS[item.category](item, reference_url, propagation_recipient_name)
    return BUILDERS[item.category](item, reference_url)


# ============================================================================
# Sprint 2 — templates & portfolios
# ============================================================================
from checkers.templates import (
    TEMPLATE_FORK, TEMPLATE_REFORMAT, TEMPLATE_TRIAGE,
)
from checkers.portfolios import (
    PORTFOLIO_METADATA, PORTFOLIO_REFORMAT,
)


_TEMPLATE_DELETE_HELP = ("To delete: open the template ▸ ⋮ (top-right) ▸ Delete template. "
                         "To rename: open it ▸ ⋮ ▸ Edit template details ▸ change the name.")


def template_fork(item, reference_url: str) -> tuple[str, str]:
    who = _first_name(item.owner_name)
    title = f'Confirm or consolidate template "{item.name}"'
    body = f"""Hi {who},

Your template "{item.name}" looks similar in name to our standard "{item.similar_to}".

Your template: {_template_url(item.gid)}

Please make a decision:
- It's a duplicate / not really needed → delete it and point your team at the PD standard.
- It's intentionally different → keep it, but rename to the convention ([TEMPLATE] - [TEAM] - Subject) and mark this complete.

{_TEMPLATE_DELETE_HELP}

Reference (standards + walkthrough): {reference_url}

Thanks!"""
    return title, body


def template_reformat(item, reference_url: str) -> tuple[str, str]:
    who = _first_name(item.owner_name)
    title = f'Review template "{item.name}" — rename or delete'
    body = f"""Hi {who},

Your template "{item.name}" doesn't match our naming convention.

Your template: {_template_url(item.gid)}

Please make a decision:
- Not needed / a leftover → delete it.
- Still used → rename it to: {item.suggested_name}
  (Standard: [TEMPLATE] - [PD or TEAM] - Subject, regular hyphens. Adjust team/subject if my guess is off.)

{_TEMPLATE_DELETE_HELP}

Reference (standards + walkthrough): {reference_url}

Why this matters: a predictable template name tells everyone which workflow they're cloning before they open it, and keeps the library scannable.

Thanks!"""
    return title, body


def template_triage(item, reference_url: str, recipient_name: str = "Robin") -> tuple[str, str]:
    who = _first_name(recipient_name)
    title = f'Triage template "{item.name}"'
    body = f"""Hi {who},

A template needs a human decision — its name is well-formed but it isn't in the approved set, and it isn't an obvious fork of one.

Template: "{item.name}" ({_template_url(item.gid)})
Owner: {item.owner_name}

Please make a decision: sanctioned addition (leave it), a duplicate to delete/consolidate, or a candidate for the approved PD set.

Reference: {reference_url}

Thanks!"""
    return title, body


def portfolio_metadata(item, reference_url: str) -> tuple[str, str]:
    who = _first_name(item.owner_name)
    missing_lines = "\n".join(f"- {m}" for m in item.missing)
    title = f'Add missing metadata to portfolio "{item.name.strip()}"'
    body = f"""Hi {who},

Your portfolio "{item.name.strip()}" is missing some required metadata:
{missing_lines}

What to do: add the missing items to the portfolio (owner and membership are set in the portfolio's properties; for sunset date and inclusion criteria, use the portfolio's designated fields).

- Sunset date (when does this stop being useful? Evergreen portfolios use "Evergreen — quarterly review")
- Inclusion criteria (one sentence on what kinds of projects belong)

Reference: {reference_url}

Why this matters: portfolios without sunset dates become graveyards. Inclusion criteria prevents scope creep.

Thanks!"""
    return title, body


def portfolio_reformat(item, reference_url: str) -> tuple[str, str]:
    who = _first_name(item.owner_name)
    title = f'Review portfolio "{item.name.strip()}" — keep & rename, or delete'
    body = f"""Hi {who},

You own this portfolio, which doesn't match our naming convention — and you may have forgotten it exists:

"{item.name.strip()}"
{_portfolio_url(item.gid)}

Please make a decision:
- Not needed anymore → delete it (open the portfolio ▸ ⋮ top-right ▸ Delete portfolio). Most stale portfolios should just go.
- Still useful → rename it to: {item.suggested_name.strip()}
  (Standard: [SCOPE] - [Subject] - [Time bound], regular hyphens. SCOPE is one of PD, X-FN, PROG, PERSONAL, or a team prefix — adjust if my guess is off.)

Reference (standards + walkthrough): {reference_url}

Why this matters: portfolios without a clear scope prefix pile up and nobody knows what's current. A quick keep/delete decision keeps reporting clean.

Thanks!"""
    return title, body


def project_misnamed(item, reference_url: str) -> tuple[str, str]:
    who = _first_name(item.owner_name)
    issues = "\n".join(f"- {i}" for i in (item.issues or []))
    title = f'Rename project "{item.name}" to match the convention'
    body = f"""Hi {who},

Your project "{item.name}" needs a quick rename to match our convention.

Your project: {_project_url(item.gid)}

Standard format: [TEAM] - [TYPE] - [Subject] - [Date or version]
(Use regular hyphens, not en-dashes.)

Suggested name: {item.suggested_name}
(Adjust TYPE / Date to fit the actual project; ?TYPE means it needs a human pick.)

What to fix:
{issues}

What to do: rename the project (Project name ▸ click to edit). If it's actually finished or a duplicate, archive or delete it instead.

Reference: {reference_url}

Why this matters: predictable names let people find your project in search, group it correctly in portfolios, and tell what kind of project it is without opening it.

Thanks!"""
    return title, body


def build_project(item, reference_url: str) -> tuple[str, str]:
    return project_misnamed(item, reference_url)


def project_cleanup(*, name: str, gid: str, owner_name: str, stale_days: int,
                    suggested_name: str | None, issues: list, reference_url: str) -> tuple[str, str]:
    """Stale-project cleanup: archive/delete if done, or keep + rename to standard.
    `suggested_name` is None when the project is already well-named."""
    who = _first_name(owner_name)
    rename_block = (f"- Still active? Keep it, but rename to match the standard: {suggested_name}\n"
                    if suggested_name else
                    "- Still active? Keep it (the name already matches the standard).\n")
    title = f'Archive or rename your stale project "{name}"'
    body = f"""Hi {who},

This project hasn't been touched in ~{stale_days} days, so it's likely clutter — and with 1,100+ active projects, stale ones are what make Asana hard to search.

"{name}"
{_project_url(gid)}

Please make a quick decision:
- Done / no longer needed? Archive it (open the project ▸ project-name dropdown ▸ Archive). Delete only if it's true junk with no history worth keeping.
{rename_block}
Standard format: [TEAM] - [TYPE] - [Subject] - [Date or version], regular hyphens.

Reference (standards + how-to): {reference_url}

Thanks — this is the single biggest thing that makes the workspace usable again."""
    return title, body


_TEMPLATE_BUILDERS = {
    TEMPLATE_FORK: template_fork,
    TEMPLATE_REFORMAT: template_reformat,
    TEMPLATE_TRIAGE: template_triage,
}
_PORTFOLIO_BUILDERS = {
    PORTFOLIO_METADATA: portfolio_metadata,
    PORTFOLIO_REFORMAT: portfolio_reformat,
}


def build_template(item, reference_url: str, triage_recipient_name: str = "Robin") -> tuple[str, str]:
    if item.category == TEMPLATE_TRIAGE:
        return _TEMPLATE_BUILDERS[item.category](item, reference_url, triage_recipient_name)
    return _TEMPLATE_BUILDERS[item.category](item, reference_url)


def build_portfolio(item, reference_url: str) -> tuple[str, str]:
    return _PORTFOLIO_BUILDERS[item.category](item, reference_url)
