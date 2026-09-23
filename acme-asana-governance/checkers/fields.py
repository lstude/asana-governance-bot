"""
checkers/fields.py — the custom-field decision tree.

Given the set of fields that are NEW this week (already filtered to user-created
by the caller) plus the full current inventory, classify each one into a
suggestion bucket:

    use_existing          → duplicates an existing standard; point at it
    rename                → no concept match, creator's team known; add prefix
    template_propagation  → 5+ same-named project-scoped copies by one person;
                            one item for the cluster, fix the source template
    triage                → no match and team unknown; a human must look

Decision order (per the spec):
    1 & 2. Exact / concept match to a standard  → use_existing
           (both collapse into the curated alias_map lookup)
    3.     No match, creator team known          → rename
    4.     No match, creator team unknown        → triage

Already-correctly-prefixed fields (PD- or a known TEAM-) are compliant and
produce no item.

This module is pure: no network, no Asana/Notion calls. It takes data in and
returns structured items out, so it is trivial to unit-test and to dry-run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from typing import Optional


# --- categories -------------------------------------------------------------
USE_EXISTING = "use_existing"
RENAME = "rename"
TEMPLATE_PROPAGATION = "template_propagation"
TRIAGE = "triage"


@dataclass
class FieldItem:
    """One actionable suggestion about one field (or one propagation cluster)."""
    category: str
    field_name: str                 # the field's display name (stripped)
    field_gid: str                  # source field GID — also the dedup key for tasks
    creator_name: str
    creator_gid: str
    scope: str                      # "global" | "project-scoped"
    on_project_count: int
    host_project_gid: str = ""      # for project-scoped: a project that hosts it
    host_project_name: str = ""

    # use_existing
    standard: Optional[str] = None          # e.g. "PD-Priority"
    standard_project_count: Optional[int] = None

    # rename
    suggested_name: Optional[str] = None    # e.g. "BIZOPS-Kickstarter"
    team: Optional[str] = None

    # template_propagation
    cluster_count: Optional[int] = None     # how many copies in the cluster
    cluster_gids: list = dc_field(default_factory=list)


def normalize(name: str) -> str:
    """Lowercase, trim, collapse internal whitespace — matches alias_map keys."""
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


def split_prefix(name: str) -> tuple[Optional[str], str]:
    """
    Return (prefix, remainder) splitting on the first hyphen.
    'BIZOPS-Status' -> ('BIZOPS', 'Status'); 'M&A-Risk Level' -> ('M&A','Risk Level');
    'Priority' -> (None, 'Priority'); 'In-Region Team Status' -> ('In','Region Team Status').
    The caller decides whether the prefix is a *known* one.
    """
    stripped = (name or "").strip()
    if "-" in stripped:
        head, rest = stripped.split("-", 1)
        return head.strip(), rest.strip()
    return None, stripped


class FieldChecker:
    def __init__(self, standards: dict, creator_to_team: dict):
        self.standards = standards
        self.alias_map = {normalize(k): v for k, v in (standards.get("alias_map") or {}).items()}
        self.workspace_prefix = standards["field_prefixes"]["workspace"]   # "PD"
        self.team_prefixes = set(standards["field_prefixes"]["teams"].keys())
        # ASA- = Asana system fields (reference-only). Recognised as a valid
        # prefix so an ASA-named field is never flagged for a rename.
        self.known_prefixes = {self.workspace_prefix.upper(), "ASA"} | {t.upper() for t in self.team_prefixes}
        self.creator_team = (creator_to_team or {}).get("by_gid", {}) or {}
        gov = standards.get("governance") or {}
        self.threshold = gov.get("template_propagation_threshold", 5)
        # Names to skip entirely (template fixed at source; clones age out).
        self.ignore_names = {normalize(n) for n in (gov.get("ignore_field_names") or [])}

    # -- helpers --------------------------------------------------------------
    def _is_known_prefix(self, prefix: Optional[str]) -> bool:
        return bool(prefix) and prefix.upper() in self.known_prefixes

    def _team_for(self, creator_gid: str) -> Optional[str]:
        team = self.creator_team.get(str(creator_gid))
        # "PD" in the map means a workspace/governance person, not a delivery team;
        # we don't auto-suggest "PD-Foo" renames (those go through the request form),
        # so treat PD-mapped creators as "team unknown" for rename purposes.
        if team and team.upper() == self.workspace_prefix.upper():
            return None
        return team

    def _standard_project_count(self, standard_name: str, inventory: list) -> int:
        target = normalize(standard_name)
        return max(
            (int(r.get("on_project_count") or 0)
             for r in inventory if normalize(r.get("name_stripped") or r.get("name")) == target),
            default=0,
        )

    # -- main -----------------------------------------------------------------
    def classify(self, new_fields: list, inventory: list) -> list:
        """
        new_fields: inventory rows that are new this week (user-created only).
        inventory:  the full current inventory (used for standard project counts).
        Returns a list of FieldItem.
        """
        if self.ignore_names:
            new_fields = [r for r in new_fields
                          if normalize(r.get("name_stripped") or r.get("name")) not in self.ignore_names]

        items: list[FieldItem] = []

        # Pass 1: detect template-propagation clusters among NEW project-scoped
        # fields grouped by (normalized name, creator). These are pulled out so
        # they don't also generate individual rename/use-existing items.
        clustered_gids: set[str] = set()
        groups: dict[tuple, list] = {}
        for r in new_fields:
            if (r.get("scope") or "") != "project-scoped":
                continue
            key = (normalize(r.get("name_stripped") or r.get("name")), str(r.get("created_by_gid")))
            groups.setdefault(key, []).append(r)

        for (norm_name, creator_gid), rows in groups.items():
            if len(rows) < self.threshold:
                continue
            first = rows[0]
            gids = [str(r.get("gid")) for r in rows]
            clustered_gids.update(gids)
            items.append(FieldItem(
                category=TEMPLATE_PROPAGATION,
                field_name=(first.get("name_stripped") or first.get("name")).strip(),
                field_gid=gids[0],                       # representative; cluster dedup key
                creator_name=first.get("created_by") or "",
                creator_gid=str(creator_gid),
                scope="project-scoped",
                on_project_count=int(first.get("on_project_count") or 0),
                cluster_count=len(rows),
                cluster_gids=gids,
            ))

        # Pass 2: classify the remaining new fields one at a time.
        for r in new_fields:
            if str(r.get("gid")) in clustered_gids:
                continue
            item = self._classify_one(r, inventory)
            if item is not None:
                items.append(item)

        return items

    def is_compliant(self, r: dict, inventory: list) -> bool:
        """True if this field needs no action — already correctly prefixed, or on
        the ignore list (template fixed at source / Asana-locked). Used by
        auto-resolution to close tasks whose field has since been handled."""
        if normalize(r.get("name_stripped") or r.get("name")) in self.ignore_names:
            return True
        return self._classify_one(r, inventory) is None

    def _classify_one(self, r: dict, inventory: list) -> Optional[FieldItem]:
        name = (r.get("name_stripped") or r.get("name") or "").strip()
        gid = str(r.get("gid"))
        creator_name = r.get("created_by") or ""
        creator_gid = str(r.get("created_by_gid") or "")
        scope = r.get("scope") or ""
        on_proj = int(r.get("on_project_count") or 0)

        prefix, _rest = split_prefix(name)

        # Already correctly prefixed → compliant, nothing to do.
        if self._is_known_prefix(prefix):
            return None

        base = FieldItem(
            category=TRIAGE, field_name=name, field_gid=gid,
            creator_name=creator_name, creator_gid=creator_gid,
            scope=scope, on_project_count=on_proj,
            host_project_gid=str(r.get("host_project_gid") or ""),
            host_project_name=(r.get("host_project_name") or ""),
        )

        # Steps 1 & 2: concept match against the curated alias map.
        standard = self.alias_map.get(normalize(name))
        if standard:
            base.category = USE_EXISTING
            base.standard = standard
            base.standard_project_count = self._standard_project_count(standard, inventory)
            return base

        # Step 3: no concept match, creator's team known → suggest a prefixed rename.
        team = self._team_for(creator_gid)
        if team:
            base.category = RENAME
            base.team = team
            base.suggested_name = f"{team}-{name}"
            return base

        # Step 4: no match, team unknown → triage.
        return base
