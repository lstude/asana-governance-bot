"""
checkers/portfolios.py — portfolio naming + metadata completeness (Sprint 2).

Standard format:  [SCOPE] – [Subject] – [Time bound if relevant]
  SCOPE ∈ {PD, X-FN, PROG, PERSONAL} ∪ team prefixes.
  (Unlike templates, SCOPE is bare here — no square brackets.)

Required metadata: owner, description, sunset_date, inclusion_criteria, membership.
  - owner, membership: native Asana portfolio properties.
  - description, sunset_date, inclusion_criteria: NOT native — stored as
    portfolio custom fields. The mapping required_metadata -> custom field name
    is config-driven (METADATA_FIELD_MAP), confirmed against a live portfolio.

Decision order (per the spec):
    1. Name matches `[SCOPE] – [Subject]`? If not → reformat suggestion.
    2. If format ok → check metadata completeness; list any missing fields.

Pure module: data in, structured items out. No network. The caller supplies
each portfolio already enriched with owner / item count / custom-field values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from typing import Optional

PORTFOLIO_REFORMAT = "portfolio_reformat"
PORTFOLIO_METADATA = "portfolio_metadata"
PORTFOLIO_COMPLIANT = "portfolio_compliant"

# required_metadata key -> how to detect it.
#   "native:owner"   → portfolio.owner present
#   "native:members" → portfolio has >=1 item (membership)
#   "cf:<Field Name>" → a portfolio custom field of that exact name has a value
#
# REALITY (confirmed against a live portfolio 2026-06-18): Asana portfolios have
# no description field, and description / sunset_date / inclusion_criteria are
# NOT stored anywhere yet. So only owner + membership are enforceable today.
# The cf: rules below are kept ready — once PD creates those portfolio custom
# fields, add the three keys to `metadata_enforced` in standards.yaml and set the
# cf names here to match.
METADATA_FIELD_MAP = {
    "owner": "native:owner",
    "membership": "native:members",
    "description": "cf:Description",
    "sunset_date": "cf:Sunset Date",
    "inclusion_criteria": "cf:Inclusion Criteria",
}


@dataclass
class PortfolioItem:
    category: str
    name: str
    gid: str
    owner_name: str
    owner_gid: str
    suggested_name: Optional[str] = None
    scope: Optional[str] = None
    missing: list = dc_field(default_factory=list)   # human-readable missing metadata


def _norm_dashes(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("–", "-").replace("—", "-")).strip()


def _segments(name: str) -> list[str]:
    return [seg.strip() for seg in _norm_dashes(name).split(" - ")]


class PortfolioChecker:
    def __init__(self, standards: dict, creator_to_team: dict,
                 metadata_field_map: dict | None = None):
        self.workspace_prefix = standards["field_prefixes"]["workspace"].upper()
        team_prefixes = {t.upper() for t in standards["field_prefixes"]["teams"].keys()}
        pn = standards.get("portfolio_naming") or {}
        scopes = {s.upper() for s in (pn.get("scopes") or [])}
        self.valid_scopes = scopes | team_prefixes | {self.workspace_prefix}
        # Only metadata we can actually verify today (owner + membership). The
        # other three have no storage yet — see METADATA_FIELD_MAP note.
        self.metadata_enforced = pn.get("metadata_enforced") or ["owner", "membership"]
        self.creator_team = (creator_to_team or {}).get("by_gid", {}) or {}
        self.metadata_field_map = metadata_field_map or METADATA_FIELD_MAP

    def _team_for(self, owner_gid: str) -> Optional[str]:
        return self.creator_team.get(str(owner_gid))

    def _infer_scope(self, name: str, owner_gid: str) -> str:
        """Best-effort scope for a reformat suggestion. Look only at the LEADING
        tokens of the first segment (e.g. 'ALL PD - …' → PD); a scope word buried
        in the subject (… NPI Launch …) must not win. Else the owner's team; else
        '?'. Portfolio scope often isn't the owner's team, so '?' is honest."""
        first_seg = _segments(name)[0] if _segments(name) else ""
        for tok in first_seg.split():
            if tok.upper() in self.valid_scopes:
                return tok.upper()
        return self._team_for(owner_gid) or "?SCOPE"

    def _missing_metadata(self, p: dict) -> list:
        """Return human-readable names of missing required metadata."""
        cfs = {(c.get("name") or "").strip(): (c.get("display_value") or "").strip()
               for c in (p.get("custom_fields") or [])}
        missing = []
        for key in self.metadata_enforced:
            rule = self.metadata_field_map.get(key, "")
            ok = True
            if rule == "native:owner":
                ok = bool((p.get("owner") or {}).get("gid") or p.get("owner_gid"))
            elif rule == "native:members":
                ok = int(p.get("item_count") or 0) > 0
            elif rule.startswith("cf:"):
                ok = bool(cfs.get(rule[3:], ""))
            if not ok:
                missing.append(key.replace("_", " ").title())
        return missing

    def classify(self, portfolios: list) -> list:
        return [it for it in self.classify_all(portfolios) if it.category != PORTFOLIO_COMPLIANT]

    def classify_all(self, portfolios: list) -> list:
        """Every portfolio classified, including compliant — for the Notion table."""
        return [self._classify_one(p) for p in portfolios]

    def _classify_one(self, p: dict) -> PortfolioItem:
        name = (p.get("name") or "").strip()
        gid = str(p.get("gid"))
        owner = p.get("owner") or {}
        owner_name = owner.get("name") or p.get("owner_name") or ""
        owner_gid = str(owner.get("gid") or p.get("owner_gid") or "")
        base = PortfolioItem(PORTFOLIO_COMPLIANT, name, gid, owner_name, owner_gid)

        segs = _segments(name)
        scope = segs[0].upper() if segs else ""
        well_formed = len(segs) >= 2 and scope in self.valid_scopes

        if not well_formed:
            inferred = self._infer_scope(name, owner_gid)
            base.category = PORTFOLIO_REFORMAT
            base.scope = inferred
            # Portfolios use regular hyphens (per the doc), so rejoin with " - ".
            subject = name if len(segs) < 2 else " - ".join(segs[1:])
            base.suggested_name = f"{inferred} - {subject}"
            return base

        base.scope = scope
        missing = self._missing_metadata(p)
        if missing:
            base.category = PORTFOLIO_METADATA
            base.missing = missing
        return base
