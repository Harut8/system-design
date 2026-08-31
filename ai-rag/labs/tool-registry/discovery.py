"""Tool Discovery — keyword search, agent-scoped filtering, ranking (design doc §5).

Three things make this more than a ``SELECT ... WHERE`` wrapper:

1. **Authorization is applied before ranking, not as a post-filter on top-K** (§5.1).
   An agent must never infer the *existence* of tools it cannot use from a truncated
   result set, and legitimate results must never be crowded out by unusable ones.

2. **Tool health feeds ranking** (§5.3).  A tool that's currently circuit-broken should
   stop being recommended even if it's the best semantic match — surfacing a tool an
   agent will immediately fail to call wastes a turn.

3. **Dependency graph exposure** (§5.4).  Discovery returns upstream dependencies so a
   planner can pre-fetch a chain in one search rather than discovering failures one hop
   at a time.

Semantic search (embeddings + vector index) is **stubbed** — it requires an embedding
model, which this lab deliberately excludes.  The keyword + structured-filter path is
fully functional and exercises every other aspect of the discovery design.
"""

from __future__ import annotations

import difflib
import fnmatch
import math
from dataclasses import dataclass, field
from typing import Any

from models import (
    DependencyRelation,
    Principal,
    Tool,
    ToolBundle,
    ToolRef,
    ToolState,
    ToolVersion,
    parse_semver,
    semver_satisfies,
)


@dataclass(frozen=True)
class SearchResult:
    """One result from a discovery search."""

    tool_ref: str  # "namespace.name"
    version: str
    tool_version_id: str
    score: float
    description: str
    annotations: dict[str, bool]
    why_matched: str  # "keyword", "tag_filter", "keyword+tag_filter"
    dependencies: list[str] = field(default_factory=list)
    commonly_paired_with: list[str] = field(default_factory=list)
    deprecated: bool = False


class DiscoveryService:
    """Discovery — keyword search, scoped filtering, ranking (§5)."""

    def __init__(self, registry: Any) -> None:
        """Takes a ``ToolRegistry`` instance to read from."""
        self._registry = registry
        # Simulated health scores and popularity (in production, fed from metrics).
        self._health_scores: dict[str, float] = {}  # tool_ref -> 0.0–1.0
        self._popularity: dict[str, int] = {}  # tool_ref -> invocations_last_30d

    # ---- Configuration ----

    def set_health_score(self, tool_ref: str, score: float) -> None:
        self._health_scores[tool_ref] = max(0.0, min(1.0, score))

    def set_popularity(self, tool_ref: str, invocations: int) -> None:
        self._popularity[tool_ref] = invocations

    # ---- Search (§5.1, §5.2) ----

    def search(
        self,
        *,
        query: str = "",
        agent: Principal | None = None,
        tags: set[str] | None = None,
        annotations_filter: dict[str, bool] | None = None,
        limit: int = 10,
    ) -> list[SearchResult]:
        """Combined keyword + structured-filter search, authorization-scoped.

        If ``agent`` is provided, results are filtered to tools the agent is
        allowed to call **before** ranking (§5.1's critical rule).
        """
        # 1. Collect all active (and deprecated) tool versions.
        candidates = self._collect_candidates()

        # 2. Authorization filter FIRST (§5.1 — never post-filter).
        if agent is not None:
            candidates = [c for c in candidates if self._is_allowed(c, agent)]

        # 3. Tag filter.
        if tags:
            candidates = [c for c in candidates if tags.issubset(c["tags"])]

        # 4. Annotation filter.
        if annotations_filter:
            candidates = [
                c for c in candidates
                if all(c["annotations"].get(k) == v for k, v in annotations_filter.items())
            ]

        # 5. Keyword matching + scoring.
        scored: list[tuple[dict, float, str]] = []
        for c in candidates:
            score, match_reason = self._score_candidate(c, query)
            if score > 0 or not query:
                scored.append((c, score, match_reason))

        # 6. Sort by score descending.
        scored.sort(key=lambda x: x[1], reverse=True)

        # 7. Build results.
        results: list[SearchResult] = []
        for c, score, match_reason in scored[:limit]:
            tv_id = c["tool_version_id"]
            deps = self._registry.get_dependencies(tv_id)
            dep_refs = [
                self._version_to_ref(d.depends_on_tool_version_id)
                for d in deps
                if d.relation == DependencyRelation.REQUIRES_OUTPUT_OF
            ]
            paired = [
                self._version_to_ref(d.depends_on_tool_version_id)
                for d in deps
                if d.relation == DependencyRelation.COMMONLY_PAIRED_WITH
            ]
            results.append(SearchResult(
                tool_ref=c["tool_ref"],
                version=c["version"],
                tool_version_id=tv_id,
                score=round(score, 3),
                description=c["description"],
                annotations=c["annotations"],
                why_matched=match_reason,
                dependencies=[r for r in dep_refs if r],
                commonly_paired_with=[r for r in paired if r],
                deprecated=c["state"] == ToolState.DEPRECATED,
            ))
        return results

    def _collect_candidates(self) -> list[dict]:
        """Flatten all discoverable tool versions into candidate dicts."""
        candidates = []
        for tool in self._registry.all_tools():
            for tv in tool.versions.values():
                if tv.state not in (ToolState.ACTIVE, ToolState.DEPRECATED):
                    continue
                ann = tv.definition.spec.annotations
                ref = f"{tool.namespace}.{tool.name}"
                candidates.append({
                    "tool_ref": ref,
                    "version": tv.semver,
                    "tool_version_id": tv.tool_version_id,
                    "description": tv.definition.spec.description,
                    "tags": tool.tags,
                    "annotations": {
                        "read_only": ann.read_only,
                        "idempotent": ann.idempotent,
                        "destructive": ann.destructive,
                        "requires_approval": ann.requires_approval,
                        "long_running": ann.long_running,
                    },
                    "state": tv.state,
                    "owner_team": tool.owner_team,
                })
        return candidates

    def _is_allowed(self, candidate: dict, agent: Principal) -> bool:
        """Check if a principal is allowed to see this tool (§5.1, §8)."""
        ref = candidate["tool_ref"]
        # Denylist always wins (§8.1.1).
        if ref in agent.denied_tools:
            return False
        # Check against allowlist patterns.
        if not agent.allowed_tool_patterns:
            return False
        return any(
            fnmatch.fnmatch(ref, pattern)
            for pattern in agent.allowed_tool_patterns
        )

    def _score_candidate(
        self,
        candidate: dict,
        query: str,
    ) -> tuple[float, str]:
        """§5.3 ranking — weighted blend (minus semantic similarity, which is stubbed)."""
        if not query:
            # No query: rank by health + popularity only.
            health = self._health_scores.get(candidate["tool_ref"], 1.0)
            pop = self._popularity.get(candidate["tool_ref"], 0)
            pop_score = math.log1p(pop) / 15.0  # normalize roughly
            score = 0.5 + 0.25 * health + 0.25 * min(pop_score, 1.0)
            return score, "browse"

        ref = candidate["tool_ref"]
        desc = candidate["description"].lower()
        tags_str = " ".join(candidate["tags"]).lower()
        q = query.lower()
        match_reason_parts = []

        # Keyword match on tool name.
        name_sim = difflib.SequenceMatcher(None, q, ref.lower()).ratio()
        if name_sim > 0.3:
            match_reason_parts.append("keyword")

        # Keyword match on description.
        desc_sim = difflib.SequenceMatcher(None, q, desc).ratio()

        # Keyword match on tags.
        tag_sim = 0.0
        for word in q.split():
            if word in tags_str:
                tag_sim = max(tag_sim, 0.8)
                if "tag_filter" not in match_reason_parts:
                    match_reason_parts.append("tag_filter")

        # Exact substring match boost.
        substring_boost = 0.0
        if q in ref.lower() or q in desc:
            substring_boost = 0.3
            if "keyword" not in match_reason_parts:
                match_reason_parts.append("keyword")

        # Health and popularity.
        health = self._health_scores.get(ref, 1.0)
        pop = self._popularity.get(ref, 0)
        pop_score = math.log1p(pop) / 15.0

        # Weighted blend (§5.3, with semantic_similarity = 0).
        score = (
            0.0  # semantic_similarity (stubbed)
            + 0.30 * max(name_sim, desc_sim, tag_sim)
            + 0.20 * substring_boost
            + 0.25 * health
            + 0.25 * min(pop_score, 1.0)
        )

        if not match_reason_parts:
            match_reason_parts = ["keyword"]

        return score, "+".join(match_reason_parts)

    def _version_to_ref(self, tool_version_id: str) -> str:
        """Look up the tool_ref for a tool_version_id."""
        tv = self._registry.get_version(tool_version_id)
        if tv is None:
            return ""
        tool = next(
            (t for t in self._registry.all_tools()
             if t.tool_id == tv.tool_id),
            None,
        )
        if tool is None:
            return ""
        return f"{tool.namespace}.{tool.name}@{tv.semver}"

    # ---- Hallucination recovery (§5 + ch.24 §5.1) ----

    def suggest(self, unknown_name: str) -> str:
        """Fuzzy match a hallucinated tool name — "did you mean?" path."""
        all_refs = [
            f"{t.namespace}.{t.name}" for t in self._registry.all_tools()
        ]
        matches = difflib.get_close_matches(unknown_name, all_refs, n=1, cutoff=0.4)
        return f"Did you mean '{matches[0]}'?" if matches else "No similar tool exists."

    # ---- Bundle resolution (§5.5) ----

    def resolve_bundle(self, bundle: ToolBundle) -> list[ToolVersion]:
        """Resolve a bundle to concrete tool versions."""
        resolved: list[ToolVersion] = []
        for ref in bundle.tools:
            tv = self._registry.resolve_version(ref.namespace, ref.name, ref.version_range)
            if tv is not None:
                resolved.append(tv)
        return resolved

    # ---- Dependency graph (§5.4) ----

    def get_dependency_chain(self, tool_version_id: str) -> list[str]:
        """Return the full upstream dependency chain as tool_refs."""
        chain_ids = self._registry.get_upstream_chain(tool_version_id)
        return [self._version_to_ref(vid) for vid in chain_ids if self._version_to_ref(vid)]

    # ---- Agent-scoped recommendations (§5.2) ----

    def for_principal(self, agent: Principal, limit: int = 20) -> list[SearchResult]:
        """Return all tools this agent can call, ranked by health + popularity."""
        return self.search(agent=agent, limit=limit)
