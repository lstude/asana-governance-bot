"""
checkers/templates.py — template naming + suspected-fork decision tree (Sprint 2).

Standard format:  [TEMPLATE] – [PD] – Subject   or   [TEMPLATE] – [TEAM] – Subject

Decision order (per the spec):
    1. Format matches `[TEMPLATE] – [PD/TEAM] – Subject`?
       - [PD]: in the approved set → compliant; else check for a suspected fork.
       - [TEAM]: sanctioned extension → compliant, unless the subject looks like
         a fork of an approved PD template (then flag: "could you use the PD one?").
    2. Format wrong → suggest a reformatted name using the owner's team.
    3. Format right but the name looks like a fork → suspected_fork.
    4. Unknown scope / no [TEMPLATE] prefix → triage.

LIMITATION (spec): fork detection is name-similarity only. A clean fork that was
fully renamed won't be caught. This is by design for Sprint 2.

Pure module: data in, structured items out. No network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Optional

TEMPLATE_COMPLIANT = "template_compliant"
TEMPLATE_REFORMAT = "template_reformat"
TEMPLATE_FORK = "template_fork"
TEMPLATE_TRIAGE = "template_triage"

FORK_KEYWORDS = ("copy", "clone", "fork", "duplicate", "(2)", " v2", "- 2")
FORK_SUBJECT_THRESHOLD = 0.55   # token-overlap ratio above which we suspect a fork


@dataclass
class TemplateItem:
    category: str
    name: str
    gid: str
    owner_name: str
    owner_gid: str
    suggested_name: Optional[str] = None
    similar_to: Optional[str] = None      # the approved PD template a fork resembles
    team: Optional[str] = None


def _norm_dashes(s: str) -> str:
    """Normalise en/em dashes to a plain hyphen and collapse whitespace."""
    return re.sub(r"\s+", " ", (s or "").replace("–", "-").replace("—", "-")).strip()


def _segments(name: str) -> list[str]:
    return [seg.strip() for seg in _norm_dashes(name).split(" - ")]


def _norm_key(s: str) -> str:
    return _norm_dashes(s).lower()


def _subject_tokens(subject: str) -> set:
    return set(re.findall(r"[a-z0-9]+", subject.lower()))


class TemplateChecker:
    def __init__(self, standards: dict, creator_to_team: dict):
        self.workspace_prefix = standards["field_prefixes"]["workspace"].upper()  # PD
        self.team_prefixes = {t.upper() for t in standards["field_prefixes"]["teams"].keys()}
        self.creator_team = (creator_to_team or {}).get("by_gid", {}) or {}
        approved = (standards.get("template_naming") or {}).get("approved_pd", []) or []
        self.approved = approved
        self.approved_keys = {_norm_key(a) for a in approved}
        # subject (3rd segment onward) of each approved PD template, for fork scoring
        self.approved_subjects = {}
        for a in approved:
            segs = _segments(a)
            if len(segs) >= 3:
                self.approved_subjects[a] = " - ".join(segs[2:])

    def _team_for(self, owner_gid: str) -> Optional[str]:
        team = self.creator_team.get(str(owner_gid))
        return team if team and team.upper() != self.workspace_prefix else None

    def _suspected_fork_of(self, subject: str) -> Optional[str]:
        """Return the approved PD template name this subject most resembles, or None.
        An explicit fork keyword (copy/clone/v2…) lowers the bar to just one
        shared word — the wording itself is the signal; similarity only names
        which template it's a fork of."""
        toks = _subject_tokens(subject)
        if not toks:
            return None
        has_keyword = any(k in subject.lower() for k in FORK_KEYWORDS)
        threshold = 0.01 if has_keyword else FORK_SUBJECT_THRESHOLD
        return self._best_subject_match(subject, toks, threshold)

    def _best_subject_match(self, subject: str, toks: set, threshold: float) -> Optional[str]:
        best_name, best_score = None, 0.0
        for name, appr_subject in self.approved_subjects.items():
            atoks = _subject_tokens(appr_subject)
            if not atoks:
                continue
            jacc = len(toks & atoks) / len(toks | atoks)
            seq = SequenceMatcher(None, subject.lower(), appr_subject.lower()).ratio()
            score = max(jacc, seq)
            if score > best_score:
                best_name, best_score = name, score
        return best_name if best_score >= threshold else None

    def classify(self, templates: list) -> list:
        """templates: list of {gid, name, owner_name, owner_gid}. Returns items
        (compliant ones omitted)."""
        return [it for it in self.classify_all(templates) if it.category != TEMPLATE_COMPLIANT]

    def classify_all(self, templates: list) -> list:
        """Every template classified, including compliant — for the Notion table."""
        return [self._classify_one(t) for t in templates]

    def _classify_one(self, t: dict) -> Optional[TemplateItem]:
        name = (t.get("name") or "").strip()
        gid = str(t.get("gid"))
        owner_name = t.get("owner_name") or ""
        owner_gid = str(t.get("owner_gid") or "")
        base = TemplateItem(TEMPLATE_TRIAGE, name, gid, owner_name, owner_gid)

        segs = _segments(name)
        well_formed = len(segs) >= 3 and segs[0].strip("[]").upper() == "TEMPLATE"

        if not well_formed:
            # Step 2: format wrong → reformat using owner's team (or PD if unknown).
            team = self._team_for(owner_gid) or self.workspace_prefix
            base.category = TEMPLATE_REFORMAT
            base.team = team
            base.suggested_name = f"[TEMPLATE] - [{team}] - {name}"
            return base

        scope = segs[1].strip("[]").upper()
        subject = " - ".join(segs[2:])

        if scope == self.workspace_prefix:        # [PD]
            if _norm_key(name) in self.approved_keys:
                base.category = TEMPLATE_COMPLIANT
                return base
            fork = self._suspected_fork_of(subject)
            if fork:
                base.category = TEMPLATE_FORK
                base.similar_to = fork
                return base
            base.category = TEMPLATE_TRIAGE       # [PD] but not approved and not an obvious fork
            return base

        if scope in self.team_prefixes:           # [TEAM]
            fork = self._suspected_fork_of(subject)
            if fork:
                base.category = TEMPLATE_FORK
                base.similar_to = fork
                base.team = scope
                return base
            base.category = TEMPLATE_COMPLIANT     # sanctioned team extension
            return base

        # Unknown scope token.
        base.category = TEMPLATE_TRIAGE
        return base
