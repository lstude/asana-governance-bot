"""
checkers/projects.py — project naming compliance (Sprint 3, report-only).

Standard:  [TEAM] - [TYPE] - [Subject] - [Date or version, if relevant]
  TEAM ∈ {MKTG, BIZOPS, WS, RTL, PRD, FIN, HR}
  TYPE ∈ {LAUNCH, SPRINT, CAMPAIGN, EVENT, ONBOARD, REVIEW, INITIATIVE, 1:1, TRADESHOW}
  Subject in Title Case. Date YYYY-MM / YYYY / vN (omit if there'll only be one).
  No status flags (WIP/DRAFT/TEST/DELETE), no emojis, no double-team naming.

Decision tree (per the spec):
  1. Parse the four parts from the current name.
  2. TEAM missing/invalid → infer from the owner's team.
  3. TYPE missing → can't infer → flag for a human.
  4. List specific violations (status flag, bad date, emoji, double team).
  5. Produce a best-effort suggested name.

Report-only: no Notion sync (1,000+ projects). New projects only (the run diffs
against the baseline). Owner-name-in-title detection is out of scope for v1
(unreliable), same spirit as template fork detection.

Pure module: data in, structured items out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from typing import Optional

PROJECT_COMPLIANT = "project_compliant"
PROJECT_MISNAMED = "project_misnamed"

# Emoji / symbol ranges that break search and don't render everywhere.
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000026FF\U00002700-\U000027BF"
    "\U0001F1E6-\U0001F1FF\U00002190-\U000021FF\U00002B00-\U00002BFF️]"
)


@dataclass
class ProjectItem:
    category: str
    name: str
    gid: str
    owner_name: str
    owner_gid: str
    team: Optional[str] = None
    suggested_name: Optional[str] = None
    issues: list = dc_field(default_factory=list)


def _norm_dashes(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("–", "-").replace("—", "-")).strip()


def _segments(name: str) -> list[str]:
    return [s.strip() for s in _norm_dashes(name).split(" - ") if s.strip()]


class ProjectChecker:
    def __init__(self, standards: dict, creator_to_team: dict):
        pn = standards.get("project_naming") or {}
        self.teams = {t.upper() for t in (pn.get("teams") or [])}
        self.types = {t.upper() for t in (pn.get("types") or [])}
        self.date_re = re.compile(pn.get("date_pattern") or r"^\d{4}(-\d{2})?$")
        self.forbidden = {f.upper() for f in (pn.get("forbidden_in_name") or [])}
        self.creator_team = (creator_to_team or {}).get("by_gid", {}) or {}

    def _team_for(self, owner_gid: str) -> Optional[str]:
        return self.creator_team.get(str(owner_gid))

    def _looks_like_date_attempt(self, seg: str) -> bool:
        """A trailing segment that is *trying* to be a date but isn't in our
        format — e.g. 'Oct 20, 2026' or '08.20.2025'. A subject that merely
        contains a year ('BFCM 2025') is NOT a date attempt: it only counts if,
        after stripping date tokens, essentially nothing is left."""
        if self.date_re.match(seg):
            return False
        months = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
        had_date_token = bool(re.search(r"\b\d{4}\b", seg) or re.search(rf"\b({months})\b", seg.lower()))
        residue = re.sub(rf"\b({months})\b", "", seg.lower())
        residue = re.sub(r"[0-9\s,.\-/]", "", residue)
        return had_date_token and residue == ""

    def classify(self, projects: list) -> list:
        return [it for it in self.classify_all(projects) if it.category != PROJECT_COMPLIANT]

    def classify_all(self, projects: list) -> list:
        return [self._classify_one(p) for p in projects]

    def _classify_one(self, p: dict) -> ProjectItem:
        name = (p.get("name") or "").strip()
        gid = str(p.get("gid"))
        owner = p.get("owner") or {}
        owner_name = owner.get("name") or p.get("owner_name") or ""
        owner_gid = str(owner.get("gid") or p.get("owner_gid") or "")
        item = ProjectItem(PROJECT_COMPLIANT, name, gid, owner_name, owner_gid)

        segs = _segments(name)
        upper_segs = [s.upper() for s in segs]
        issues: list[str] = []

        # emoji / special characters
        if _EMOJI_RE.search(name) or "|" in name:
            issues.append("Remove emojis / special characters (they break search)")

        # status flags anywhere in the name
        words = {w.upper() for w in re.findall(r"[A-Za-z]+", name)}
        for flag in sorted(self.forbidden & words):
            issues.append(f'Remove the "{flag}" status flag from the title (use the Status field)')

        # TEAM (first segment) — infer from owner if absent/invalid
        team = upper_segs[0] if upper_segs and upper_segs[0] in self.teams else None
        idx = 1 if team else 0
        if team is None:
            inferred = self._team_for(owner_gid)
            issues.append(f"Add TEAM prefix ({inferred or '?TEAM'})")
            team_for_suggestion = inferred or "?TEAM"
        else:
            team_for_suggestion = team

        # double-team
        if len([s for s in upper_segs if s in self.teams]) > 1:
            issues.append("Don't name the team twice")

        # TYPE (segment right after team) — can't infer
        ptype = upper_segs[idx] if idx < len(upper_segs) and upper_segs[idx] in self.types else None
        if ptype is None:
            issues.append("Add a TYPE (LAUNCH / SPRINT / CAMPAIGN / EVENT / ONBOARD / "
                          "REVIEW / INITIATIVE / 1:1 / TRADESHOW)")

        # subject + trailing date
        body = segs[idx + 1:] if ptype else segs[idx:]
        date = None
        if body and self.date_re.match(body[-1]):
            date = body[-1]
            body = body[:-1]
        elif body and self._looks_like_date_attempt(body[-1]):
            issues.append(f'Fix the date format on "{body[-1]}" (use YYYY-MM, YYYY, or vN)')
            body = body[:-1]
        # drop any status-flag segments from the subject so the suggestion is clean
        body = [s for s in body if s.upper() not in self.forbidden]
        subject = " - ".join(body) if body else (name if not segs else "")

        if not issues:
            item.team = team
            return item

        # build a best-effort suggested name
        parts = [team_for_suggestion, ptype or "?TYPE", subject or "?Subject"]
        suggested = " - ".join(parts)
        if date:
            suggested += f" - {date}"
        item.category = PROJECT_MISNAMED
        item.team = team or self._team_for(owner_gid)
        item.suggested_name = suggested
        item.issues = issues
        return item
