# Asana Governance Bot

> **About this repo.** This is a genericized version of a governance system I
> built and ran in production at a ~80-person company, published with the
> company's permission. All user GIDs, people, workspace IDs and baseline data
> here are samples — the standards file, team prefixes and configs are stand-ins
> you would replace with your own. The code and architecture are as they ran.


A weekly, fully-automated system that keeps the Asana workspace, the Notion
governance doc, and the team in alignment. It detects drift in custom fields
(Sprint 1), suggests the right correction, files pre-assigned cleanup tasks in
Asana, posts a digest to Slack, and refreshes the Notion library tables.

Runs unattended via GitHub Actions every Monday morning. No one has to run a
script.

## What it does each Monday

1. Pulls a fresh inventory from Asana (`pull_inventory_complete.py` for fields;
   `asana/pull_entities.py` for templates/portfolios/projects).
2. Diffs each entity type against last week's snapshot (`baseline/inventory_latest.json`)
   so it flags only what's **newly created/changed**.
3. Filters fields to **user-created** (ASA- system fields are reference-only).
4. Runs the decision trees (`checkers/`) → a suggestion per item: use an existing
   standard, rename to a prefix, template-propagation cluster, fork, or triage.
5. **Auto-resolves** any open cleanup task whose field is now compliant or deleted
   (closes it with a comment) — the system cleans up after itself.
6. Creates cleanup tasks in the Governance Cleanup project, **pre-assigned to the
   creator/owner**, Robin as follower, due in 7 days, with a by-impact
   PD-Priority. Idempotent. A person with ≥2 of their own fixes gets **one parent
   task + a subtask each** (not N separate tasks).
7. Posts a single Slack digest (`slack/compose.py`).
8. Refreshes the Notion library tables between `{{...}}` markers (`sync/notion_sync.py`):
   PD-SET + team libraries always; templates/portfolios when Sprint 2 is on.
9. Writes the new baseline so next Monday diffs against this week.

## Operating model (read this)

- **Live by default.** In CI the run creates tasks for real. Pass `--dry-run`
  only for local testing; production never uses it.
- **New-only.** Each entity is diffed against the baseline, so people are flagged
  for what they newly create — never re-nagged for the backlog. The first run per
  entity just establishes the baseline (no opening-day burst).
- **Self-healing.** Tasks are idempotent (typed `governance-id` markers), and
  auto-resolution closes tasks once the work happens (via our task or someone
  fixing it directly). Flaky Asana endpoints get retry/backoff; Actions emails on
  failure.
- **Grouping.** Per-person bundling keeps My Tasks sane; tune `batch_threshold`
  in `config/standards.yaml`.
- **Priority.** `priority_mode: auto` stamps PD-Priority by impact (map in
  standards.yaml); set `michelle` to leave it blank for manual triage.

## Operating modes (manual dispatch)

Run from the Actions tab ("Weekly Asana Governance Run" → Run workflow) with inputs:

| Input | Effect |
|---|---|
| `sprint2` | also check templates + portfolios |
| `dry_run` | compute only; write nothing |
| `sweep` | one-time backfill — classify ALL current items, ignore baseline (skips Notion + baseline write) |
| `sweep_target` | what the sweep covers: `fields,templates,portfolios,projects,all`. `projects` = a **stale-project** sweep (archive/delete-or-rename, threshold `stale_project_days`) |
| `creator` | with a fields sweep, restrict to one person (exact display name) |

Every run also **auto-resolves** done tasks and posts a **friendly nudge** comment
on tasks open longer than `nudge_after_days`.

Locally: `python governance_run.py [--dry-run] [--sprint2] [--sweep] [--creator "Name"] [--inventory CSV]`.

## 🛑 Kill switch — how to stop it if it goes haywire

The system runs unattended on a schedule, so if something looks wrong (a flood of
bad tasks, a bad run, etc.) and the owner is out, **disable it first, diagnose
later.** Nothing compounds while it's off, and turning it back on loses nothing
(baselines are intact).

**1. Fastest — turn the workflow off (no code, ~10 seconds):**
- GitHub → repo → **Actions** tab → **Weekly Asana Governance Run** (left sidebar)
  → top-right **`•••`** → **Disable workflow**.
- This stops the Monday cron *and* manual runs. Nothing runs while disabled — no
  Asana tasks, no Notion writes, no Slack. Re-enable the same way (**Enable workflow**).
- CLI equivalent:
  `gh workflow disable "Weekly Asana Governance Run" --repo acme-cosf/pd-asana-governance`
  (re-enable with `gh workflow enable ...`).

**2. A run is mid-flight and misbehaving:** Actions tab → click the running job →
**Cancel workflow**. (Task creation is resilient + idempotent, so a half-run is safe.)

**3. Nuclear option (revoke access):** in repo **Settings → Secrets and variables
→ Actions**, delete or rotate **`ASANA_SERVICE_TOKEN`** (and/or `NOTION_TOKEN`,
`SLACK_WEBHOOK_URL`). Runs then fail safely (they create nothing) until restored.
Use only if disabling the workflow isn't enough.

**Cleaning up bad tasks**, if a run created junk: they all live in the Governance
Cleanup project and carry a hidden `governance-id:` marker in their notes. Delete
them there; the system won't recreate ones whose underlying item is gone, and
dedup means re-running never double-files.

**Re-enabling after the dust settles:** flip the workflow back on. The next Monday
(or a manual dispatch) resumes from the existing baseline — no catch-up flood.

## Status

Sprints 1–2 live. Custom fields run every Monday; templates/portfolios available
via `sprint2`. Sprint 3 (projects, report-only) in progress. See `asana_governance_spec.md`.

## Setup

### Secrets (GitHub repo → Settings → Secrets and variables → Actions)

| Secret | Purpose |
|---|---|
| `ASANA_SERVICE_TOKEN` | Asana PAT (admin). Same var the inventory script reads. |
| `NOTION_TOKEN` | Notion integration token. |
| `NOTION_GOVERNANCE_PAGE_ID` | Governance page UUID. |
| `ASANA_CLEANUP_PROJECT_GID` | Governance Cleanup project the tasks land in. |
| `SLACK_WEBHOOK_URL` | Incoming webhook for the digest. *(pending IT)* |

### One-time Notion prep

Paste the marker snippet from `config/notion_markers.md` into the governance
doc, once, under *Custom field naming standards*. The sync finds and replaces
between the markers — it never searches by header text.

## Local testing

```bash
pip install -r requirements.txt
export ASANA_SERVICE_TOKEN="0/..."        # admin PAT
export NOTION_TOKEN="secret_..."
export NOTION_GOVERNANCE_PAGE_ID="..."
export ASANA_CLEANUP_PROJECT_GID="9537610396283960"
# SLACK_WEBHOOK_URL optional locally — without it, the digest is written to ./artifacts

python governance_run.py --dry-run        # pulls + computes, writes NOTHING to Asana/Notion
```

`--dry-run` prints the planned tasks and writes the Slack JSON + inventory CSV
to `./artifacts/` so you can eyeball everything before it goes live.

## Layout

```
governance_run.py            orchestrator (pull → diff → suggest → act)
pull_inventory_complete.py   field inventory pull (pre-existing, untouched)
checkers/fields.py           field decision tree
asana/task_creator.py        idempotent cleanup-task creation
asana/task_templates.py      plain-English task descriptions per category
slack/compose.py             Block Kit digest builder
sync/notion_sync.py          marker-based PD-SET table refresh
config/standards.yaml        machine-readable mirror of the Notion standards
config/creator_to_team.yaml  creator → team-prefix mapping
config/notion_markers.md     snippet to paste into Notion once
baseline/inventory_latest.json   last week's snapshot (for diffing)
```
