"""
slack/compose.py — builds the weekly Block Kit digest and posts it.

Sprint 1 reports the custom-field section only. Because the weekly run creates
the Asana tasks itself (pre-assigned to each field's creator), the digest is a
*notification*, not an action surface: each line deep-links to the task that was
filed, so Robin can click through to reassign or comment if needed. No
interactive buttons — an incoming webhook can't receive button clicks anyway.

Empty buckets are omitted. A clean week produces a short, friendly message.

`post()` sends to the webhook when SLACK_WEBHOOK_URL is set; otherwise the
caller writes the JSON to ./artifacts for inspection (the webhook is pending IT).
"""

from __future__ import annotations

import json
from urllib import request, error

from checkers.fields import (
    USE_EXISTING, RENAME, TEMPLATE_PROPAGATION, TRIAGE,
)
from checkers.templates import (
    TEMPLATE_FORK, TEMPLATE_REFORMAT, TEMPLATE_TRIAGE,
)
from checkers.portfolios import (
    PORTFOLIO_METADATA, PORTFOLIO_REFORMAT,
)
from checkers.projects import PROJECT_MISNAMED

BUCKET_ORDER = [
    (USE_EXISTING,        "🗑️ Use existing standard"),
    (RENAME,              "✏️ Suggested rename"),
    (TEMPLATE_PROPAGATION, "🔁 Template propagation"),
    (TRIAGE,              "❓ Needs triage"),
]


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _divider() -> dict:
    return {"type": "divider"}


def _task_link(dedup_gid: str, task_results: dict) -> str:
    """Return ' · <url|→ task>' if a task was created for this item, else ''."""
    res = task_results.get(dedup_gid) or {}
    url = res.get("permalink")
    if url:
        return f" · <{url}|→ task>"
    if res.get("status") == "skipped":
        return " · _(task already open)_"
    if res.get("status") == "dry-run":
        return " · _(task would be created)_"
    return ""


def _line_for(item, task_results: dict) -> str:
    link = _task_link(item.field_gid, task_results)
    if item.category == USE_EXISTING:
        return f'• "{item.field_name}" (by {item.creator_name}) → use *{item.standard}*{link}'
    if item.category == RENAME:
        return f'• "{item.field_name}" (by {item.creator_name}) → *{item.suggested_name}*{link}'
    if item.category == TEMPLATE_PROPAGATION:
        return (f'• {item.cluster_count} new "{item.field_name}" project-scoped copies by '
                f'{item.creator_name} — fix the source template{link}')
    if item.category == TRIAGE:
        scope = "project-scoped, may be fine" if item.scope == "project-scoped" else "global"
        return f'• "{item.field_name}" (by {item.creator_name}) — {scope}{link}'
    return f'• "{item.field_name}"{link}'


def _field_summary(items: list) -> str:
    counts = {}
    for it in items:
        counts[it.category] = counts.get(it.category, 0) + 1
    parts = []
    if counts.get(RENAME):
        parts.append(f"{counts[RENAME]} rename" + ("s" if counts[RENAME] != 1 else ""))
    if counts.get(USE_EXISTING):
        parts.append(f"{counts[USE_EXISTING]} duplicate" + ("s" if counts[USE_EXISTING] != 1 else ""))
    if counts.get(TEMPLATE_PROPAGATION):
        parts.append(f"{counts[TEMPLATE_PROPAGATION]} template propagation")
    if counts.get(TRIAGE):
        parts.append(f"{counts[TRIAGE]} to triage")
    return ", ".join(parts) if parts else "nothing new"


TEMPLATE_BUCKET_ORDER = [
    (TEMPLATE_FORK,     "⚠️ Possible fork"),
    (TEMPLATE_REFORMAT, "✏️ Suggested rename"),
    (TEMPLATE_TRIAGE,   "❓ Needs triage"),
]
PORTFOLIO_BUCKET_ORDER = [
    (PORTFOLIO_METADATA, "📋 Missing metadata"),
    (PORTFOLIO_REFORMAT, "✏️ Suggested rename"),
]


def _template_line(it, task_results: dict) -> str:
    link = _task_link(it.gid, task_results)
    if it.category == TEMPLATE_FORK:
        return f'• "{it.name}" (by {it.owner_name}) — similar to *{it.similar_to}*{link}'
    if it.category == TEMPLATE_REFORMAT:
        return f'• "{it.name}" (by {it.owner_name}) → *{it.suggested_name}*{link}'
    return f'• "{it.name}" (by {it.owner_name}){link}'


def _portfolio_line(it, task_results: dict) -> str:
    link = _task_link(it.gid, task_results)
    if it.category == PORTFOLIO_METADATA:
        return f'• "{it.name.strip()}" — missing: {", ".join(it.missing)}{link}'
    return f'• "{it.name.strip()}" → *{it.suggested_name.strip()}*{link}'


def _project_line(it, task_results: dict) -> str:
    link = _task_link(it.gid, task_results)
    return f'• "{it.name}" (by {it.owner_name}) → *{it.suggested_name}*{link}'


def build_message(*, run_date: str, items: list, task_results: dict | None = None,
                  resolved: list | None = None, aging: list | None = None,
                  report_url: str | None = None,
                  template_items: list | None = None,
                  portfolio_items: list | None = None,
                  project_items: list | None = None) -> dict:
    """
    items:           list[FieldItem] from the field checker.
    template_items:  list[TemplateItem] (Sprint 2).
    portfolio_items: list[PortfolioItem] (Sprint 2).
    task_results:    {gid: result dict} from TaskCreator.create().
    resolved/aging:  lists of strings.
    Returns a Slack message dict ({"blocks": [...], "text": fallback}).
    """
    task_results = task_results or {}
    resolved = resolved or []
    aging = aging or []
    template_items = template_items or []
    portfolio_items = portfolio_items or []
    project_items = project_items or []

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"🗂️ Weekly governance check — {run_date}"}},
    ]
    if report_url:
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"<{report_url}|Governance Cleanup project>"}]})

    if not any([items, template_items, portfolio_items, project_items, resolved, aging]):
        blocks.append(_section("✅ *All clear this week.* Nothing drifted from standard."))
        return {"blocks": blocks, "text": f"Weekly governance check — {run_date}: all clear"}

    if items:
        blocks.append(_divider())
        blocks.append(_section(f"🏷️ *CUSTOM FIELDS* ({_field_summary(items)})"))
        for category, label in BUCKET_ORDER:
            bucket = [it for it in items if it.category == category]
            if not bucket:
                continue
            lines = "\n".join(_line_for(it, task_results) for it in bucket)
            blocks.append(_section(f"*{label}:*\n{lines}"))

    if template_items:
        blocks.append(_divider())
        blocks.append(_section(f"📐 *TEMPLATES* ({len(template_items)} flagged)"))
        for category, label in TEMPLATE_BUCKET_ORDER:
            bucket = [it for it in template_items if it.category == category]
            if not bucket:
                continue
            lines = "\n".join(_template_line(it, task_results) for it in bucket)
            blocks.append(_section(f"*{label}:*\n{lines}"))

    if portfolio_items:
        blocks.append(_divider())
        blocks.append(_section(f"📁 *PORTFOLIOS* ({len(portfolio_items)} flagged)"))
        for category, label in PORTFOLIO_BUCKET_ORDER:
            bucket = [it for it in portfolio_items if it.category == category]
            if not bucket:
                continue
            lines = "\n".join(_portfolio_line(it, task_results) for it in bucket)
            blocks.append(_section(f"*{label}:*\n{lines}"))

    if project_items:
        blocks.append(_divider())
        blocks.append(_section(f"📋 *PROJECTS* ({len(project_items)} misnamed)"))
        blocks.append(_section("*✏️ Suggested rename:*\n"
                               + "\n".join(_project_line(it, task_results) for it in project_items)))

    if resolved:
        blocks.append(_divider())
        blocks.append(_section("✅ *Resolved this week:*\n" + "\n".join(f"• {r}" for r in resolved)))

    if aging:
        blocks.append(_divider())
        blocks.append(_section("⏰ *Aging (open >30 days):*\n" + "\n".join(f"• {a}" for a in aging)))

    parts = []
    if items:
        parts.append(_field_summary(items))
    if template_items:
        parts.append(f"{len(template_items)} template(s)")
    if portfolio_items:
        parts.append(f"{len(portfolio_items)} portfolio(s)")
    if project_items:
        parts.append(f"{len(project_items)} project(s)")
    fallback = f"Weekly governance check — {run_date}: " + ("; ".join(parts) or "see details")
    return {"blocks": blocks, "text": fallback}


def post(webhook_url: str, message: dict) -> None:
    """POST the message to a Slack incoming webhook. Raises on non-200."""
    data = json.dumps(message).encode("utf-8")
    req = request.Request(webhook_url, data=data, method="POST",
                          headers={"Content-Type": "application/json"})
    try:
        with request.urlopen(req, timeout=20) as r:
            body = r.read().decode("utf-8", errors="replace")
            if r.status != 200 or body.strip() != "ok":
                raise RuntimeError(f"Slack webhook returned {r.status}: {body}")
    except error.HTTPError as e:
        raise RuntimeError(f"Slack webhook HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}") from e
