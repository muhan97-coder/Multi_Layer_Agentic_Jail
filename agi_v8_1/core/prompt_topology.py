"""R15 prefix-cache topology helpers.

Provides utilities to split a swarm lane prompt into a static prefix block
(byte-stable across all lanes in a dispatch) and a dynamic per-lane suffix.
DeepSeek-V4-Pro server-side prefix cache requires byte-identical prefixes
to register a cache hit.

Worker A sole owner: __R15_SLOT_A__
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence


@dataclass(frozen=True)
class PromptBlock:
    static: bool
    content: str


STATUS_AVAILABLE = "available"
STATUS_ADVISORY = "advisory"
STATUS_UNAVAILABLE = "unavailable"
STATUS_NOT_REQUIRED = "not_required"


@dataclass(frozen=True)
class TopologyAgent:
    """One explicit contract row for an agent, sentinel, or compatibility prompt."""

    name: str
    group: str
    tier: str
    prompt_ref: str = "none"
    cadence: str = ""
    definition_required: bool = True
    prompt_required: bool = True
    phase_roles: tuple[str, ...] = field(default_factory=tuple)
    lifecycle: str = "active"
    note: str = ""

    @property
    def prompt_id(self) -> str:
        return normalize_prompt_ref(self.prompt_ref)


@dataclass(frozen=True)
class TopologyResolution:
    """Availability view for one contract row."""

    name: str
    group: str
    definition_status: str
    prompt_status: str
    execution_status: str
    advisory_only: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "group": self.group,
            "definition_status": self.definition_status,
            "prompt_status": self.prompt_status,
            "execution_status": self.execution_status,
            "advisory_only": self.advisory_only,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class TopologyContract:
    """Canonical topology contract used by defs, prompts, and phase executors."""

    entries: tuple[TopologyAgent, ...]

    def by_name(self) -> dict[str, TopologyAgent]:
        return {entry.name: entry for entry in self.entries}

    def get(self, name: str) -> TopologyAgent | None:
        return self.by_name().get(name)

    def phase_entry_names(self, phase_role: str) -> tuple[str, ...]:
        return tuple(
            entry.name for entry in self.entries if phase_role in entry.phase_roles
        )

    def prompt_ids(self) -> tuple[str, ...]:
        seen: set[str] = set()
        out: list[str] = []
        for name in default_prompt_template_names():
            if name not in seen:
                seen.add(name)
                out.append(name)
        for entry in self.entries:
            prompt_id = entry.prompt_id
            if entry.prompt_required and prompt_id and prompt_id not in seen:
                seen.add(prompt_id)
                out.append(prompt_id)
        return tuple(out)

    def required_definition_names(self) -> tuple[str, ...]:
        return tuple(entry.name for entry in self.entries if entry.definition_required)

    def resolve_agent(
        self,
        name: str,
        *,
        definition_names: Iterable[str] | None = None,
        prompt_ids: Iterable[str] | None = None,
    ) -> TopologyResolution:
        entry = self.get(name)
        if entry is None:
            return TopologyResolution(
                name=name,
                group="unknown",
                definition_status=STATUS_UNAVAILABLE,
                prompt_status=STATUS_UNAVAILABLE,
                execution_status=STATUS_UNAVAILABLE,
                advisory_only=True,
                reasons=(f"{name}: not declared in topology contract",),
            )

        definition_set = set(definition_names) if definition_names is not None else None
        prompt_set = {normalize_prompt_ref(p) for p in prompt_ids} if prompt_ids is not None else None
        reasons: list[str] = []

        if entry.definition_required:
            if definition_set is None:
                definition_status = STATUS_ADVISORY
                reasons.append(f"{entry.name}: definition source not supplied")
            elif entry.name in definition_set:
                definition_status = STATUS_AVAILABLE
            else:
                definition_status = STATUS_UNAVAILABLE
                reasons.append(f"{entry.name}: required agent definition is missing")
        else:
            definition_status = STATUS_ADVISORY
            reasons.append(f"{entry.name}: definition is advisory/not required")

        if not entry.prompt_required or not entry.prompt_id:
            prompt_status = STATUS_NOT_REQUIRED
        elif prompt_set is None:
            prompt_status = STATUS_ADVISORY
            reasons.append(f"{entry.name}: prompt source not supplied")
        elif entry.prompt_id in prompt_set:
            prompt_status = STATUS_AVAILABLE
        else:
            prompt_status = STATUS_UNAVAILABLE
            reasons.append(
                f"{entry.name}: required prompt {entry.prompt_ref!r} is missing"
            )

        if entry.lifecycle == "active":
            execution_status = STATUS_AVAILABLE if entry.phase_roles else STATUS_ADVISORY
            if not entry.phase_roles:
                reasons.append(f"{entry.name}: no phase executor role declared")
        elif entry.lifecycle == "removed":
            execution_status = STATUS_ADVISORY
            reasons.append(f"{entry.name}: removed from active topology")
        else:
            execution_status = STATUS_ADVISORY
            reasons.append(f"{entry.name}: {entry.lifecycle} topology entry")

        advisory_only = any(
            status != STATUS_AVAILABLE
            for status in (definition_status, execution_status)
        ) or prompt_status not in (STATUS_AVAILABLE, STATUS_NOT_REQUIRED)

        return TopologyResolution(
            name=entry.name,
            group=entry.group,
            definition_status=definition_status,
            prompt_status=prompt_status,
            execution_status=execution_status,
            advisory_only=advisory_only,
            reasons=tuple(reasons),
        )

    def resolve_all(
        self,
        *,
        definition_names: Iterable[str] | None = None,
        prompt_ids: Iterable[str] | None = None,
    ) -> tuple[TopologyResolution, ...]:
        return tuple(
            self.resolve_agent(
                entry.name,
                definition_names=definition_names,
                prompt_ids=prompt_ids,
            )
            for entry in self.entries
        )


def normalize_prompt_ref(prompt_ref: str | object) -> str:
    """Return the contract prompt id for ``foo.txt`` / ``foo.md`` style refs."""

    raw = str(prompt_ref or "").strip()
    if raw.lower() in {"", "none"}:
        return ""
    raw = raw.replace("\\", "/").rsplit("/", 1)[-1]
    lower = raw.lower()
    for suffix in (".rules.txt", ".txt", ".md"):
        if lower.endswith(suffix):
            return raw[: -len(suffix)]
    return raw


def default_prompt_template_names() -> tuple[str, ...]:
    """Compatibility prompt ids, ordered as the legacy PromptLoader exposed them."""

    return (
        "architect",
        "continuation",
        "critic",
        "digestive_agent",
        "head_strategist",
        "prompt_engineer",
        "self_improvement",
        "support_advisor",
        "timesfm_skill",
        "worker_builder",
        "worker_tester",
    )


def default_topology_contract() -> TopologyContract:
    """Return the canonical V8 topology contract."""

    A = TopologyAgent
    return TopologyContract(
        entries=(
            A(
                "session_bootstrap",
                "controller",
                "support",
                prompt_required=False,
                cadence="session_start_only",
                phase_roles=("session_bootstrap",),
                note="code-backed session setup controller",
            ),
            A(
                "executor",
                "controller",
                "controller",
                prompt_required=False,
                definition_required=False,
                cadence="every_cycle",
                phase_roles=("worker_execute",),
                lifecycle="sentinel",
                note="code executor, not an LLM agent definition",
            ),
            A("strategist", "head", "head", "head_strategist.txt", "every_cycle", phase_roles=("head",)),
            A("critic", "head", "head", "critic.txt", "every_5th_cycle", phase_roles=("review",)),
            A("continuation", "head", "head", "continuation.txt", "every_cycle", phase_roles=("review",)),
            A(
                "self_improvement",
                "head",
                "head",
                "self_improvement.txt",
                "every_cycle",
                phase_roles=("review_optional",),
                lifecycle="advisory",
                note="optional SI dispatcher; missing definition must be explicit",
            ),
            A("builder", "worker", "worker", "worker_builder.txt", "every_cycle", phase_roles=("worker",)),
            A("tester", "worker", "worker", "worker_tester.txt", "every_cycle", phase_roles=("worker",)),
            A("digestive", "support", "support", "digestive_agent.txt", "every_cycle", phase_roles=("support",)),
            *(
                A(
                    name,
                    "arm",
                    "metadata",
                    prompt_required=False,
                    definition_required=False,
                    lifecycle="metadata",
                    note="metadata-only functional arm",
                )
                for name in (
                    "intake_arm",
                    "planning_arm",
                    "context_arm",
                    "build_arm",
                    "execute_arm",
                    "verify_arm",
                    "recovery_arm",
                    "memory_arm",
                )
            ),
            *(
                A(
                    name,
                    "removed",
                    "removed",
                    prompt_required=False,
                    definition_required=False,
                    lifecycle="removed",
                    note="legacy artifact retained only for compatibility",
                )
                for name in (
                    "skeptic",
                    "scribe",
                    "handoff_generator",
                    "memory_writer",
                )
            ),
            *(
                A(
                    name,
                    "compat_prompt",
                    "compat",
                    prompt_ref=f"{prompt_id}.txt",
                    definition_required=False,
                    lifecycle="advisory",
                    note="legacy prompt-loader compatibility entry",
                )
                for name, prompt_id in (
                    ("architect", "architect"),
                    ("prompt_engineer", "prompt_engineer"),
                    ("support_advisor", "support_advisor"),
                    ("timesfm_skill", "timesfm_skill"),
                )
            ),
        )
    )


def phase_topology_status(
    phase_role: str,
    *,
    provided_names: Iterable[str] = (),
    synthetic_names: Iterable[str] = (),
    contract: TopologyContract | None = None,
) -> dict[str, object]:
    """Return a small advisory payload for phase-executor results."""

    topo = contract or default_topology_contract()
    expected = topo.phase_entry_names(phase_role)
    provided = tuple(provided_names)
    synthetic = tuple(synthetic_names)
    missing = tuple(name for name in expected if name not in provided)
    synthetic_status = {
        name: topo.resolve_agent(name).to_dict()
        for name in synthetic
    }
    return {
        "phase_role": phase_role,
        "expected_agents": list(expected),
        "provided_agents": list(provided),
        "missing_agents": list(missing),
        "synthetic_artifacts": synthetic_status,
        "advisory_only": bool(missing or synthetic_status),
    }


def split_static_dynamic(blocks: Sequence[PromptBlock]) -> tuple[str, str]:
    """Concatenate all static blocks first, then dynamic blocks.

    Returns (static_prefix, dynamic_tail). Static blocks MUST come first in
    the model input. The split allows callers to send static_prefix as
    system prompt and dynamic_tail as user content.

    All static blocks are concatenated in sequence order; all dynamic blocks
    are concatenated in sequence order (preserving relative ordering within
    each group).
    """
    static_parts: list[str] = []
    dynamic_parts: list[str] = []
    for block in blocks:
        if block.static:
            static_parts.append(block.content)
        else:
            dynamic_parts.append(block.content)
    return "".join(static_parts), "".join(dynamic_parts)


def assert_prefix_stable(prompts: list[str], min_ratio: float = 0.90) -> dict:
    """Assert that all prompts share at least ``min_ratio`` of bytes as common prefix.

    Computes the longest common prefix length across all prompts (byte-level,
    UTF-8 encoded), then divides by the minimum prompt byte length.

    Returns a dict with keys:
      - ``prefix_length`` (int): byte length of the longest common prefix.
      - ``min_prompt_length`` (int): minimum byte length across all prompts.
      - ``ratio`` (float): ``prefix_length / min_prompt_length`` (0.0 if empty).
      - ``stable`` (bool): whether ``ratio >= min_ratio``.
      - ``first_divergence_offset`` (int): byte offset where the first divergence
        occurs (-1 if all prompts are identical or the list has 0-1 entries).
    """
    if not prompts:
        return {
            "prefix_length": 0,
            "min_prompt_length": 0,
            "ratio": 1.0,
            "stable": True,
            "first_divergence_offset": -1,
        }

    encoded = [p.encode("utf-8") for p in prompts]
    min_len = min(len(b) for b in encoded)

    # Find the longest common prefix byte-by-byte.
    prefix_len = 0
    for i in range(min_len):
        byte_val = encoded[0][i]
        if all(b[i] == byte_val for b in encoded[1:]):
            prefix_len = i + 1
        else:
            break
    # If all bytes matched through min_len, prefix_len == min_len.
    # Handle edge case where loop ended without a break when all match.
    if min_len > 0 and prefix_len == 0:
        # No common byte found at position 0.
        first_div = 0
    elif prefix_len == min_len:
        first_div = -1
    else:
        first_div = prefix_len

    ratio = (prefix_len / min_len) if min_len > 0 else 1.0
    stable = ratio >= min_ratio

    return {
        "prefix_length": prefix_len,
        "min_prompt_length": min_len,
        "ratio": ratio,
        "stable": stable,
        "first_divergence_offset": first_div,
    }


def measure_cache_hit_potential(prompts: list[str]) -> dict:
    """Diagnostic: estimate prefix cache hit ratio given a list of lane prompts.

    Computes how many bytes are shared (the common prefix) vs the per-prompt
    unique tail. The estimated hit ratio is ``shared_bytes / avg_total_bytes``.

    Returns a dict with keys:
      - ``count`` (int): number of prompts.
      - ``shared_bytes`` (int): byte length of the longest common prefix.
      - ``avg_unique_bytes`` (float): average bytes beyond the shared prefix.
      - ``estimated_hit_ratio`` (float): fraction of average prompt bytes that
        are shared (0.0 if no prompts or all prompts are empty).
    """
    if not prompts:
        return {
            "count": 0,
            "shared_bytes": 0,
            "avg_unique_bytes": 0.0,
            "estimated_hit_ratio": 0.0,
        }

    encoded = [p.encode("utf-8") for p in prompts]
    min_len = min(len(b) for b in encoded)

    # Compute longest common prefix length.
    prefix_len = 0
    for i in range(min_len):
        byte_val = encoded[0][i]
        if all(b[i] == byte_val for b in encoded[1:]):
            prefix_len = i + 1
        else:
            break

    avg_total = sum(len(b) for b in encoded) / len(encoded)
    avg_unique = sum(max(0, len(b) - prefix_len) for b in encoded) / len(encoded)
    hit_ratio = (prefix_len / avg_total) if avg_total > 0 else 0.0

    return {
        "count": len(prompts),
        "shared_bytes": prefix_len,
        "avg_unique_bytes": avg_unique,
        "estimated_hit_ratio": hit_ratio,
    }
