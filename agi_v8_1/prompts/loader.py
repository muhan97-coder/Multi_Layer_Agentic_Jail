"""PromptLoader for agi_v8_1 (R23 W2 port).

Ports the loader pattern from
agi_v7.1/agent_system/core/prompt_loader.py — but with **no dependency** on
the heavy ``PromptContextService`` machinery. v8 starts with a simple
contract:

  - Each agent has a registered ``PromptTemplate`` (name + base + tags).
  - ``PromptLoader.load_prompt(name)`` returns the composed string:
      ``base + "\\n\\n" + extra_rules_joined``
    where ``extra_rules`` comes either from ``prompts_dir/<name>.rules.txt``
    (one rule per line) or — if ``prompts_dir`` is None — the empty list.
  - When ``prompts_dir`` is provided and contains ``<name>.md`` / ``.txt``,
    the file content overrides the registered base.

This matches v7.1's "raw file fallback" affordance without pulling in
PromptContextService. Env knob ``AGI_V8_PROMPTS_ENABLED`` is honoured for
any future *write* path; the read path is always available so tests pass
even with the knob OFF.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from agi_v8_1.core.prompt_topology import (
    STATUS_ADVISORY,
    STATUS_AVAILABLE,
    STATUS_NOT_REQUIRED,
    STATUS_UNAVAILABLE,
    TopologyContract,
    default_prompt_template_names,
    default_topology_contract,
    normalize_prompt_ref,
)

# __SLOT_FAIL_FAST_2026_07_25__ Swallowed failures route through one choke
# point: counted + named always, re-raised under AGI_V8_STRICT_FAIL_FAST.
from agi_v8_1.policy.fail_fast import swallowed as _swallowed

# Agents present in v7.1/prompts/. Embedded names only; full template
# content is intentionally minimal here (callers wanting v7.1's prose
# inject prompts_dir at construction time).
AGENT_NAMES: tuple[str, ...] = default_prompt_template_names()


@dataclass(frozen=True)
class PromptTemplate:
    """Immutable prompt template registration."""

    name: str
    base: str
    tags: tuple[str, ...] = field(default_factory=tuple)

    def with_rules(self, rules: list[str]) -> str:
        if not rules:
            return self.base
        bullets = "\n".join(f"- {r}" for r in rules if r.strip())
        if not bullets:
            return self.base
        return f"{self.base}\n\n# Learned rules\n{bullets}"


@dataclass(frozen=True)
class PromptResolution:
    """Non-throwing prompt resolution result."""

    requested_name: str
    prompt_id: str
    status: str
    advisory_only: bool
    text: str = ""
    path: Path | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "requested_name": self.requested_name,
            "prompt_id": self.prompt_id,
            "status": self.status,
            "advisory_only": self.advisory_only,
            "text": self.text,
            "path": str(self.path) if self.path is not None else "",
            "reason": self.reason,
        }


# Minimal core templates — one-line scaffolds (real content in v7.1 prompts/
# .md files). Tests assert presence of every AGENT_NAMES key.
DEFAULT_TEMPLATES: Mapping[str, PromptTemplate] = {
    name: PromptTemplate(
        name=name,
        base=f"You are the {name} agent in agi_v8_1. Follow protocol.",
        tags=("v8", name),
    )
    for name in AGENT_NAMES
}


def prompts_enabled() -> bool:
    return os.getenv("AGI_V8_PROMPTS_ENABLED", "false").lower() in {"1", "true", "yes"}  # tier: T2


class PromptLoader:
    """Load agent prompts with optional disk-template overrides.

    Parameters
    ----------
    prompts_dir:
        Optional directory containing ``<agent>.md`` / ``.txt`` overrides
        and ``<agent>.rules.txt`` learned-rules sidecars.
    templates:
        Override the embedded ``DEFAULT_TEMPLATES`` registry.
    """

    def __init__(
        self,
        prompts_dir: str | Path | None = None,
        *,
        templates: Mapping[str, PromptTemplate] | None = None,
        topology_contract: TopologyContract | None = None,
    ) -> None:
        self._dir = Path(prompts_dir).expanduser().resolve() if prompts_dir is not None else None
        self._templates: dict[str, PromptTemplate] = dict(templates or DEFAULT_TEMPLATES)
        self._contract = topology_contract or default_topology_contract()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def known_agents(self) -> list[str]:
        return sorted(self._templates.keys())

    def has(self, agent_name: str) -> bool:
        prompt_id = self._prompt_id(agent_name)
        return bool(prompt_id) and (prompt_id in self._templates or self._disk_has(prompt_id))

    def known_prompt_ids(self) -> tuple[str, ...]:
        seen: set[str] = set()
        out: list[str] = []
        for name in self._contract.prompt_ids():
            if name in self._templates and name not in seen:
                seen.add(name)
                out.append(name)
        for name in sorted(self._templates):
            if name not in seen:
                seen.add(name)
                out.append(name)
        if self._dir is not None and self._dir.is_dir():
            for path in sorted(self._dir.iterdir()):
                if not path.is_file():
                    continue
                if path.name.endswith(".rules.txt"):
                    continue
                if path.suffix not in {".md", ".txt"}:
                    continue
                prompt_id = normalize_prompt_ref(path.name)
                if prompt_id and prompt_id not in seen:
                    seen.add(prompt_id)
                    out.append(prompt_id)
        return tuple(out)

    def load_prompt(self, agent_name: str) -> str:
        """Return the composed prompt text for *agent_name*.

        Composition order:
          1. base = disk override (if ``prompts_dir/<name>.md|.txt`` exists)
             else the registered template's ``base``.
          2. learned rules from ``prompts_dir/<name>.rules.txt`` appended.
        """
        resolution = self.resolve_prompt(agent_name)
        if resolution.status == STATUS_UNAVAILABLE:
            raise KeyError(f"Unknown agent prompt: {agent_name!r}")
        return resolution.text

    def resolve_prompt(self, agent_name: str) -> PromptResolution:
        """Resolve without pretending embedded fallbacks are real prompt files."""

        raw_name = str(agent_name or "").strip()
        if "/" in raw_name or "\\" in raw_name:
            return PromptResolution(
                requested_name=raw_name,
                prompt_id="",
                status=STATUS_UNAVAILABLE,
                advisory_only=True,
                reason="prompt names must be simple filenames, not paths",
            )
        prompt_id = self._prompt_id(agent_name)
        if not prompt_id:
            return PromptResolution(
                requested_name=raw_name,
                prompt_id="",
                status=STATUS_NOT_REQUIRED,
                advisory_only=False,
                reason="no prompt required",
            )

        path = self.load_prompt_path(prompt_id)
        if path is not None:
            try:
                base = path.read_text(encoding="utf-8").rstrip()
            except OSError as exc:
                _swallowed(exc, site="prompts.loader.resolve_prompt:201", category="telemetry")
                return PromptResolution(
                    requested_name=str(agent_name),
                    prompt_id=prompt_id,
                    status=STATUS_UNAVAILABLE,
                    advisory_only=True,
                    path=path,
                    reason=f"prompt file unreadable: {exc}",
                )
            return PromptResolution(
                requested_name=str(agent_name),
                prompt_id=prompt_id,
                status=STATUS_AVAILABLE,
                advisory_only=False,
                text=self._compose(base, self._load_rules(prompt_id)),
                path=path,
            )

        template = self._templates.get(prompt_id)
        if template is None:
            return PromptResolution(
                requested_name=str(agent_name),
                prompt_id=prompt_id,
                status=STATUS_UNAVAILABLE,
                advisory_only=True,
                reason="prompt is not declared by the topology contract or disk",
            )

        status = STATUS_ADVISORY
        reason = "using embedded fallback template; no disk prompt resolved"
        if prompt_id not in self._contract.prompt_ids():
            reason = "using compatibility template outside the active topology"
        return PromptResolution(
            requested_name=str(agent_name),
            prompt_id=prompt_id,
            status=status,
            advisory_only=True,
            text=template.with_rules(self._load_rules(prompt_id)),
            reason=reason,
        )

    def load_prompt_path(self, agent_name: str) -> Path | None:
        """Return the on-disk path if a ``<name>.md|.txt`` override exists."""
        if self._dir is None:
            return None
        prompt_id = self._prompt_id(agent_name)
        if not prompt_id:
            return None
        for ext in (".md", ".txt"):
            p = self._dir / f"{prompt_id}{ext}"
            if p.is_file() and p.resolve().parent == self._dir:
                return p
        return None

    def register(self, template: PromptTemplate) -> None:
        if not isinstance(template, PromptTemplate):
            raise TypeError("expected PromptTemplate")
        self._templates[normalize_prompt_ref(template.name)] = template

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _compose(base: str, rules: list[str]) -> str:
        if not rules:
            return base
        bullets = "\n".join(f"- {r}" for r in rules if r.strip())
        return f"{base}\n\n# Learned rules\n{bullets}" if bullets else base

    @staticmethod
    def _prompt_id(agent_name: str) -> str:
        raw = str(agent_name or "").strip()
        if "/" in raw or "\\" in raw:
            return ""
        prompt_id = normalize_prompt_ref(raw)
        if prompt_id in {"", ".", ".."}:
            return prompt_id
        if "/" in prompt_id or "\\" in prompt_id:
            return ""
        return prompt_id

    def _disk_has(self, agent_name: str) -> bool:
        return self.load_prompt_path(agent_name) is not None

    def _load_base(self, agent_name: str) -> str:
        path = self.load_prompt_path(agent_name)
        if path is not None:
            try:
                return path.read_text(encoding="utf-8").rstrip()
            except OSError as _ff_exc:
                _swallowed(_ff_exc, site="prompts.loader._load_base:291", category="telemetry")
                pass
        template = self._templates.get(agent_name)
        if template is None:
            raise KeyError(agent_name)
        return template.base

    def _load_rules(self, agent_name: str) -> list[str]:
        if self._dir is None:
            return []
        rules_path = self._dir / f"{agent_name}.rules.txt"
        if not rules_path.is_file():
            return []
        try:
            text = rules_path.read_text(encoding="utf-8")
        except OSError as _ff_exc:
            _swallowed(_ff_exc, site="prompts.loader._load_rules:306", category="telemetry")
            return []
        return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
