# Asana Governance — Operator's Guide (plain language)

For Robin and anyone covering while Alex is out. **No code or GitHub needed
for day-to-day.** This explains what the system does, your ~5-minute weekly role,
how to answer the questions you'll get, and how to turn it off if anything looks
wrong.

---

## What this is, in one paragraph

An automated assistant that keeps Asana tidy. **Every Monday morning it scans the
workspace** for things that don't match our naming standards — custom fields,
templates, portfolios, and projects — and files friendly **cleanup tasks** to the
people who own those items, in the **Governance Cleanup** Asana project. It posts
a short summary to the **#asana-governance** Slack channel. It only *suggests* —
people make the actual changes themselves.

## What happens every Monday (automatic — nobody triggers it)

- ~8am PT it runs on its own.
- A summary lands in **#asana-governance**.
- New cleanup tasks appear in **Governance Cleanup**, already assigned to the right
  person, due in 7 days, each with step-by-step instructions and a direct link.
- It **auto-closes** tasks people already handled, and posts a gentle **nudge**
  comment on anything still open after ~2 weeks.

## Robin's weekly role (~5 minutes)

1. Read the Slack summary.
2. Glance at the new tasks in Governance Cleanup. **They're already assigned — you
   don't have to assign them.**
3. Fix the occasional oddball: something assigned to the wrong person, or a task
   that says **"?TEAM"** (the system couldn't guess the team — just pick the right
   one or reassign to whoever owns it).
4. Done. People work their own tasks; you're just air-traffic control.

## Answering the questions you'll get

- **"What is this task / why did I get it?"** → They created or own something that
  doesn't match our Asana naming standards, or a project of theirs has gone stale.
  The task explains exactly what to do and links to the item. Point them to the
  task description and the Naming Standards Notion doc.
- **"I can't find this field."** → It's *project-scoped* (lives on one project, not
  in the global field library). The task has a **direct link to the project** and
  steps to edit it there.
- **"I already fixed it."** → Great — have them mark the task complete. It also
  auto-closes on the next Monday run.
- **"Do I really have to?"** → It keeps Asana searchable and reporting accurate for
  everyone. Each task is ~2 minutes.
- **"Should this field be global or project-scoped?"** → Prefer **global** (visible
  org-wide). A true one-off they'll use once or twice can stay project-scoped. Either
  way, rename it to standard. (The task says this too.)

## What you do NOT need to worry about

- **It won't spam.** It never files the same item twice and closes tasks once done.
- **It won't change anything itself.** It only suggests; humans make edits.
- **A missed week is harmless.** It picks up where it left off.

## 🛑 If something looks wrong — turn it off, then ask

If you see a flood of odd tasks or anything looks broken, **disable it first; nothing
is lost and you can turn it back on anytime.**

1. GitHub → repo **acme-cosf/pd-asana-governance** → **Actions** tab → **Weekly
   Asana Governance Run** (left list) → **`•••`** (top right) → **Disable workflow**.
2. That stops everything — no tasks, no Slack, no Notion updates.
3. Re-enable the same way (**Enable workflow**) whenever you're ready; it resumes
   cleanly.

(If a run is happening *right now* and misbehaving: Actions → click the running job
→ **Cancel workflow**.) Full details are in the repo's README under "Kill switch."

**It is always safe to leave it off** until someone who can look into it is available.

## Heads-up: a big one-time backlog just landed

We ran a one-time sweep, so **~30 people currently have cleanup tasks** (a few have
many). That's expected — it's the *existing* mess surfaced all at once. From here it
only flags *new* drift each week, which is small. A quick heads-up to the team that
these tasks are coming (and why) helps it not feel like a surprise from a bot.

## Where things live

| Thing | Where |
|---|---|
| Cleanup tasks | **Governance Cleanup** Asana project (sections: Custom Fields / Templates / Portfolios / Projects) |
| Weekly summary | **#asana-governance** Slack channel |
| The standards + field search | **Asana Naming Standards & Governance** Notion doc |
| The code / on-off switch | GitHub `acme-cosf/pd-asana-governance` (only for rule changes or disabling) |

## Who to go to

- **Tactical and process questions during leave:** Robin
- **Change the rules or fix the code:** needs someone comfortable with the repo, or
  wait for Alex. **If unsure, use the off switch** — that's always the safe move.
