"""Pydantic v2 schemas — single source of truth for swarm_v8 data contracts."""
from __future__ import annotations

from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator


_FROZEN = ConfigDict(frozen=True, extra="forbid")


# __SLOT_W3A2__ R26 240-mode layer literal. Additive — v1 callers stay on the
# narrow "A"/"B"/"meta" subset via the `Literal["A","B","meta"]` aliases below.
# Order MUST match the kernel's upstream-pod count (Pod A + Pod B + router +
# si + digestive + handoff = 6 in 240 mode) so reviewers can grep by name.
LaneLayer = Literal[
    "router",
    "A",
    "B",
    "meta",
    "si",
    "digestive",
    "handoff",
]


class GridCell(BaseModel):
    model_config = _FROZEN
    schema_version: Literal["swarm_v8_grid_cell_v1"] = "swarm_v8_grid_cell_v1"
    cell_id: str
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float
    centroid_lat: float
    centroid_lon: float


class TaskInputs(BaseModel):
    model_config = _FROZEN
    # __SLOT_W3A2__ v2 schema sibling — additive Optional fields preserve v1
    # round-tripping (default ``None`` is byte-identical when serialized).
    schema_version: Literal[
        "swarm_v8_task_inputs_v1",
        "swarm_v8_task_inputs_v2",
    ] = "swarm_v8_task_inputs_v1"
    ndvi_ref: str | None = None
    chirps_ref: str | None = None
    isda_ref: str | None = None
    population_ref: str | None = None
    # __SLOT_W3A2__ 240-mode additive fields — wired by cross-cutting topology.
    si_seed_state_ref: str | None = None
    digestive_capacity_hint: float | None = None
    handoff_target_lane: str | None = None


# __SLOT_W3A7__ TaskBundle = user-supplied task spec loaded from JSON file.
# Threaded through `swarm_v8 simulate --task-file PATH` so the executor's
# prompt construction can pick up the real task goal / variables /
# constraints instead of the hard-coded malaria default inside
# RealDeepseekExecutor. Default-OFF: simulate without --task-file behaves
# byte-identically to the pre-W3A7 surface (W4.L10 mock-mode invariant
# preserved). Independent of the W3A2 240-mode work — additive new model,
# does not touch TaskInputs / GridTask / LaneAssignment shape.
class TaskBundle(BaseModel):
    model_config = _FROZEN
    schema_version: Literal["swarm_v8_task_bundle_v1"] = "swarm_v8_task_bundle_v1"
    task_id: str
    title: str
    goal: str
    # __SLOT_W5A2__ Generic schema-driven dispatch (replaces W5.A1 mode enum).
    #
    # The executor stays domain-agnostic: it does NOT branch on ``mode`` and
    # carries no hardcoded per-domain prompts beyond the legacy GIS default
    # that serves bundle=None callers. To support a new domain, callers
    # ship a fresh ``TaskBundle`` JSON with ``prompt_system`` +
    # ``prompt_user_template`` + (optionally) ``response_schema`` +
    # ``response_field_map`` — no executor code edits required.
    #
    # * ``response_schema`` — JSON Schema dict describing the shape of the
    #   LLM JSON response. When provided, the generic parser validates each
    #   top-level key against the schema's ``properties[<key>].type`` (the
    #   parser is permissive: an unrecognized key is dropped, a malformed
    #   value is coerced or dropped per type, never raises). When omitted,
    #   the parser still works — every top-level key is type-sniffed against
    #   the default LaneResult-known fields (weights→dict, variance→float,
    #   files→list[FileArtifact]).
    # * ``response_field_map`` — explicit mapping from response top-level
    #   key → LaneResult field name. When omitted, the identity-default map
    #   applies: ``{"weights": "selected_weights", "variance": "variance",
    #   "files": "files"}``. Custom domains override by passing a fresh map
    #   (e.g. ``{"theorems": "files", "confidence": "variance"}`` so a
    #   theorem-proving response carries lemmas in LaneResult.files and the
    #   downstream 6-axis scorer treats ``confidence`` as the variance proxy).
    #
    # Default values of ``None`` preserve byte-identical pre-W5A2 behavior
    # for every existing callsite (W3.A7 / W4.A1 / W4.A3 regression
    # snapshots stay green); the legacy ``{weights, variance}`` parse path
    # fires when no schema/map is provided.
    response_schema: dict | None = None
    response_field_map: dict[str, str] | None = None
    # Optional scale knobs — when present they are advisory hints that the
    # CLI compares against the resolved SwarmConfig totals (mismatch surfaces
    # as a validation warning, not a hard fail, so the loader stays decoupled
    # from W3A2 240-mode lane-shape choices).
    scale: int | None = None
    pod_lane_count: int | None = None
    meta_lane_count: int | None = None
    grid_task_count: int | None = None
    partial_threshold: float | None = None
    max_usd: float | None = None
    # Data references threaded into TaskInputs on each GridTask (advisory —
    # mock executor ignores; real executor pulls into prompt context).
    data_refs: TaskInputs = TaskInputs()
    variables: tuple[str, ...] = ()
    design_contract: tuple[str, ...] = ()
    axes: tuple[str, ...] = ()
    strict_requirements: tuple[str, ...] = ()
    # Prompt overrides — when set, RealDeepseekExecutor uses these in place
    # of the hard-coded GIS template. Mock executor still ignores prompts
    # (deterministic-by-seed contract).
    prompt_system: str | None = None
    prompt_user_template: str | None = None
    # __SLOT_W7A7__ Per-layer system prompt override.  Keys are LaneLayer
    # values ("A" / "B" / "router" / "si" / "digestive" / "handoff" / "meta")
    # mapped to a layer-specific system prompt fragment.  When a key is
    # present, RealDeepseekExecutor._system_prompt(task) uses it for that
    # layer — overriding both ``prompt_system`` (which is layer-blind) and
    # the executor's hardcoded ``_SYSTEM_PROMPTS`` defaults.  Missing keys
    # fall through to the existing 3-tier precedence
    # (prompt_system → _SYSTEM_PROMPTS[layer] → _SYSTEM_PROMPT default), so
    # partial maps (e.g. only ``router`` + ``digestive``) work without
    # forcing the caller to supply all 7.  Default ``None`` preserves
    # byte-identical pre-W7A7 behavior for every existing bundle (W5.A2 +
    # W3.A8 regression baselines stay green).  This is the schema-driven
    # surface that turns 4 cross-cutting layers into a true division of
    # labor (router=route, si=audit, digestive=compress, handoff=integrate)
    # for each new domain — no executor edits required.
    layer_prompts: dict[str, str] | None = None
    # __SLOT_W7A8__ Per-LANE role schema (v7.1 R14.5.a port).
    #
    # ``role_distribution`` is a 2-level dict:
    #   { layer_key ("A"|"B"|"meta"|"router"|"si"|"digestive"|"handoff"):
    #     { role_name: lane_count, ... } }
    # Insertion order of the inner dict matters — ``resolve_lane_role``
    # walks the running sum to map each lane_idx to its role slot, so
    # ``{"controller": 2, "task_graph_planner": 5}`` assigns lanes 0-1
    # to ``controller`` and lanes 2-6 to ``task_graph_planner``.
    #
    # ``role_prompts`` maps each role_name to the system-prompt fragment
    # that role should receive.  When a lane's resolved role appears in
    # this dict, it OVERRIDES ``layer_prompts`` (which is per-layer, not
    # per-role) and ``prompt_system`` (which is layer-blind).
    #
    # Default ``None`` for both preserves byte-identical pre-W7A8 behavior
    # — every existing bundle keeps its W7.A7 / W5.A2 / W3.A8 baseline.
    # Partial maps work: a bundle that only supplies role_prompts for
    # Pod A roles (no B/meta/router roles) leaves those lanes on the
    # layer_prompts → prompt_system → defaults fallback chain.
    #
    # When ``role_distribution`` is None but ``role_prompts`` is set, the
    # default v7.1-schema distribution from ``roles.DEFAULT_ROLE_DISTRIBUTION``
    # is used.  This lets a bundle ship just ``role_prompts`` (the
    # interesting part) and inherit the v7.1 16-role layout automatically.
    role_distribution: dict[str, dict[str, int]] | None = None
    role_prompts: dict[str, str] | None = None
    # __SLOT_W6A1__ Generic per-task output-token cap. Resolution order at
    # ``RealDeepseekExecutor`` instantiation (see ``cli._cmd_simulate``):
    #     CLI flag ``--max-output-tokens`` > bundle.max_output_tokens > 500.
    # Default ``None`` preserves byte-identical pre-W6A1 behavior (W3.A8 +
    # W5.A2 baselines stay green). The raised cap is OPT-IN per task —
    # the executor never carries a domain-specific hardcoded high default.
    # Positive-int validator below (must be > 0 if set).
    max_output_tokens: int | None = None
    notes: str | None = None
    # __SLOT_W8_PREV_RUN_CONTEXT_2026_05_31__ — optional handoff hint pointing
    # at a previous run's artifact directory.  Threaded through
    # ``task_input_loader._maybe_load_previous_run_context`` when the
    # AGI_V8_PREV_RUN_CONTEXT_ENABLED env gate is on (default OFF).  Default
    # None preserves byte-identical pre-W8 bundle shape — any existing JSON
    # bundle parses unchanged.
    previous_run_id: str | None = None
    # __SLOT_W7A10_2026_06_06__ — Markdown passthrough mode.
    # When True, the executor + provider chain SKIPS JSON enforcement:
    #   * DeepSeekProvider.generate omits ``response_format: {type: json_object}``
    #   * provider's system prompt does NOT prepend JSON_ONLY_SYSTEM_PROMPT
    #     (replaced by a markdown-only directive)
    #   * executor's ``_parse_response_generic`` short-circuits — stores the
    #     raw content as ``raw_response={"markdown": <text>}`` without
    #     ``json.loads`` (so half-streamed markdown does NOT JSONDecodeError)
    # Default False preserves byte-identical pre-W7A10 behavior (every existing
    # bundle keeps the JSON contract). Used for planning / critique / long-form
    # bundles where the lane should emit prose, not a structured object.
    # Discovery context: seoul_urban_dynamics_v2 round 2 critique surfaced the
    # need — 78% lane failure due to streaming + parser JSON enforcement on
    # ~20KB markdown bodies wrapped in JSON.
    markdown_passthrough: bool = False

    @field_validator("goal")
    @classmethod
    def _check_goal(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("TaskBundle.goal must be non-empty")
        if len(v.encode()) > 16 * 1024:
            raise ValueError("TaskBundle.goal exceeds 16KB limit")
        return v

    @field_validator("task_id")
    @classmethod
    def _check_task_id(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("TaskBundle.task_id must be non-empty")
        if len(v) > 128:
            raise ValueError("TaskBundle.task_id exceeds 128 chars")
        return v

    @field_validator("title")
    @classmethod
    def _check_title(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("TaskBundle.title must be non-empty")
        if len(v) > 256:
            raise ValueError("TaskBundle.title exceeds 256 chars")
        return v

    # __SLOT_W6A1__ Positive-int validator for the optional per-task cap.
    # ``None`` means "not specified" (executor uses CLI flag or 500 default).
    # Zero / negative / non-int / bool values are rejected so a malformed
    # bundle never collapses to an unusable cap (which would degrade
    # silently to 1-token completions).
    #
    # ``mode="before"`` is REQUIRED here: Pydantic v2 coerces ``True``→1 /
    # ``False``→0 / floats with integral value → int BEFORE the default
    # ``mode="after"`` validator runs, which would let ``max_output_tokens=True``
    # silently parse as ``1`` (a 1-token cap — catastrophic for the codegen
    # use case). Running in ``before`` mode lets us reject bool / float /
    # non-int strings at the raw-input boundary.
    @field_validator("max_output_tokens", mode="before")
    @classmethod
    def _check_max_output_tokens(cls, v: object) -> int | None:
        if v is None:
            return None
        # ``bool`` is an ``int`` subclass — explicit reject before the
        # general int check so a "true"/"false" typo in the bundle JSON
        # surfaces clearly instead of becoming a 1-token cap.
        if isinstance(v, bool):
            raise ValueError(
                "TaskBundle.max_output_tokens must be a positive int (got bool)"
            )
        if not isinstance(v, int):
            raise ValueError(
                "TaskBundle.max_output_tokens must be a positive int (got "
                f"{type(v).__name__})"
            )
        if v <= 0:
            raise ValueError(
                f"TaskBundle.max_output_tokens must be > 0 (got {v})"
            )
        return v


class GridTask(BaseModel):
    model_config = _FROZEN
    # __SLOT_W3A2__ schema_version widens to v2 sibling; v1 callers stay valid.
    schema_version: Literal[
        "swarm_v8_grid_task_v1",
        "swarm_v8_grid_task_v2",
    ] = "swarm_v8_grid_task_v1"
    task_id: str
    cell: GridCell
    # __SLOT_W3A2__ pod widens to LaneLayer in 240 mode. 192-mode callers
    # still emit only "A"/"B"/"meta" — the Literal superset is structural.
    pod: LaneLayer
    lane_id: str
    inputs: TaskInputs
    # __SLOT_W3A2__ additive 240-mode tagging — preserves v1 default behavior.
    layer_kind: LaneLayer | None = None
    cross_cutting_seq: int | None = None
    # __SLOT_W7A1__ Cross-cutting evidence queue. When the GridTask is a
    # cross-cutting lane (router/si/digestive/handoff), this carries the
    # sampled Pod A/B LaneResults the lane is supposed to CONSUME (not just
    # placeholder cell coordinates — see W4.L4 audit "no fan-out, no
    # workers" finding). Pod A/B tasks always leave this empty so the W3.A2
    # 192-mode byte-identical regression stays green.
    #
    # Type quoting (forward ref via str) is intentional: ``LaneResult`` is
    # defined later in the file, and Pydantic v2 resolves the forward ref
    # at model build time. The tuple default ``()`` is the additive-default
    # mechanism that lets pre-W7A1 callers (and the byte-identical v1
    # round-trip) construct ``GridTask`` without specifying evidence.
    evidence: tuple["LaneResult", ...] = ()


class LaneAssignment(BaseModel):
    model_config = _FROZEN
    # __SLOT_W3A2__ schema_version widens to v2 sibling; v1 callers stay valid.
    schema_version: Literal[
        "swarm_v8_lane_assignment_v1",
        "swarm_v8_lane_assignment_v2",
    ] = "swarm_v8_lane_assignment_v1"
    pod_a_tasks: tuple[GridTask, ...]
    pod_b_tasks: tuple[GridTask, ...]
    meta_lane_ids: tuple[str, ...]
    # __SLOT_W3A2__ 240-mode additive — default empty tuples preserve v1 shape.
    router_lane_ids: tuple[str, ...] = ()
    si_lane_ids: tuple[str, ...] = ()
    digestive_lane_ids: tuple[str, ...] = ()
    handoff_lane_ids: tuple[str, ...] = ()
    router_tasks: tuple[GridTask, ...] = ()
    si_tasks: tuple[GridTask, ...] = ()
    digestive_tasks: tuple[GridTask, ...] = ()
    handoff_tasks: tuple[GridTask, ...] = ()
    meta_tasks: tuple[GridTask, ...] = ()


class Topology(BaseModel):
    model_config = _FROZEN
    schema_version: Literal[
        "swarm_v8_topology_v1",
        "swarm_v8_topology_v2",
    ] = "swarm_v8_topology_v1"
    cells: tuple[GridCell, ...]
    assignment: LaneAssignment
    assignment_seed: int


class FileArtifact(BaseModel):
    model_config = _FROZEN
    schema_version: Literal["swarm_v8_file_artifact_v1"] = "swarm_v8_file_artifact_v1"
    path: str
    mode: Literal["create", "update", "delete"]
    content: str

    @field_validator("content")
    @classmethod
    def _check_size(cls, v: str) -> str:
        if len(v.encode()) > 64 * 1024:
            raise ValueError("FileArtifact content exceeds 64KB limit")
        return v


class LaneResult(BaseModel):
    model_config = _FROZEN
    # __SLOT_W3A2__ schema_version widens to v2 sibling; v1 callers stay valid.
    schema_version: Literal[
        "swarm_v8_lane_result_v1",
        "swarm_v8_lane_result_v2",
    ] = "swarm_v8_lane_result_v1"
    lane_id: str
    task_id: str
    # __SLOT_W3A2__ pod widens to LaneLayer to admit router/si/digestive/handoff.
    pod: LaneLayer
    # __SLOT_W3A2__ status widens to admit SI quarantine + handoff throttle.
    status: Literal[
        "ok",
        "pruned_leak",
        "pruned_multicollinearity",
        "pruned_budget",
        "pruned_si_quarantine",
        "pruned_handoff_throttle",
        "error",
    ]
    selected_weights: Mapping[str, float]
    variance: float
    files: tuple[FileArtifact, ...]
    cost_usd_estimate: float
    wallclock_ms: int
    error: str | None
    # __SLOT_LANE_SUMMARY_2026_06_14__ LLM-emitted, <=100-char natural-language
    # per-lane summary. Default "" keeps byte-identical v1 output: a lane that
    # emits no ``summary`` key downstream-renders exactly as before (the
    # mechanical top_weights drivers). It is a bounded NL note (signal-safe),
    # NOT raw codegen/markdown, so it MAY ride in the LANE_DONE event.
    summary: str = ""
    # __SLOT_W3A2__ 240-mode additive — default preserves v1 semantics.
    layer: LaneLayer | None = None
    quarantine_reason: str | None = None
    handoff_target: str | None = None
    # __SLOT_W7A6__ Optional raw parsed provider response.  When the bundle's
    # response_schema produces top-level fields that don't fit selected_weights
    # (e.g. tournament predictions with `winner: str`), executors stash the
    # full parsed dict here so the CLI ``--write-lane-jsonl`` dump path can
    # surface every domain field.  Default None preserves byte-identical
    # v1 output for any caller that does not opt in.
    raw_response: dict | None = None
    # __SLOT_W7_THINKING_FIX_2026_05_30__ — surface DeepSeek reasoning token
    # count (provider.last_usage["reasoning_tokens"]) onto the lane so the
    # post-run jsonl can prove thinking-mode activation. Default None
    # preserves byte-identical output for the legacy direct-SDK path which
    # does not populate it.
    reasoning_tokens: int | None = None
    # __SLOT_W8_CONTINUATION_RING_2026_05_31__ — optional ring tail surfacing.
    # Carries the last N (cycle,status,decision,timestamp) entries when the
    # AGI_V8_CONTINUATION_RING_ENABLED env gate is on (default OFF → empty
    # tuple, byte-identical pre-W8 output).  Sibling-additive field — never
    # bumps schema_version, never touches state/store.py per Lane E.
    status_history_tail: tuple[dict, ...] = ()
    # __SLOT_W8_DRIFT_KEYS_2026_05_31__ — optional policy drift surfacing.
    # Default empty tuple; populated by ReviewPhaseExecutorV8 when the
    # AGI_V8_POLICY_DRIFT_KEYS_ENABLED env gate is on AND the injected
    # cycle_policy exposes drift_keys()/current_snapshot+previous_snapshot.
    policy_drift_keys: tuple[str, ...] = ()


class PodReport(BaseModel):
    model_config = _FROZEN
    schema_version: Literal[
        "swarm_v8_pod_report_v1",
        "swarm_v8_pod_report_v2",
    ] = "swarm_v8_pod_report_v1"
    # __SLOT_W3A2__ pod widens to LaneLayer (router/si/digestive/handoff also
    # emit PodReports in 240 mode). 192-mode callers stay on "A"/"B" subset.
    pod: LaneLayer
    lane_results: tuple[LaneResult, ...]
    aggregated_weights: Mapping[str, float]
    variance_distribution: tuple[float, ...]
    pruned_count: int
    total_cost_usd: float
    # __SLOT_P0A_ELIGIBILITY_2026_08_17__ Observability for the "did every
    # lane this Pod was supposed to run actually land?" question. Both
    # default to the pre-P0A values (``None`` / ``0``), so every existing
    # ``PodReport(...)`` call site (tests + kernel.py's own pre-P0A shape)
    # keeps constructing byte-identical objects without passing these.
    #
    # ``expected_lane_count`` — how many lane coroutines ``_run_pod`` /
    # ``_run_cross_cutting_layer`` dispatched via ``asyncio.gather`` for this
    # Pod. ``None`` means "not measured" (legacy caller, or the
    # AGI_V8_SWARM_POD_ELIGIBILITY_ENABLED gate was OFF when this report was
    # built) — never treated as "expected zero".
    #
    # ``internal_error_count`` — how many of those lane coroutines raised
    # OUTSIDE ``_run_lane``'s own executor try/except (e.g. a ``KernelHooks``
    # callback raised) and were previously dropped silently by
    # ``asyncio.gather(..., return_exceptions=True)`` + ``continue`` at
    # kernel.py's two gather sites. 0 is both "gate off" and "gate on, no
    # lane crashed" — the distinguishing signal lives in
    # ``expected_lane_count`` being ``None`` vs. an int.
    #
    # Eligibility itself is NOT stored here as a trusted bool — see
    # ``sandbox_matrix._pod_is_eligible``, which recomputes it structurally
    # from ``lane_results``/``internal_error_count``/``expected_lane_count``
    # every time, so a PodReport built by hand (as most existing tests do,
    # with ``lane_results=()``) is judged the same way as one the kernel
    # aggregated — trusting a stored ``eligible=True`` default would silently
    # readmit exactly the empty-Pod case this track exists to close.
    expected_lane_count: int | None = None
    internal_error_count: int = 0


# __SLOT_W7A1__ Resolve the GridTask.evidence forward ref now that LaneResult
# is defined. Pydantic v2 requires an explicit model_rebuild() when a
# self-referential forward ref crosses a class boundary; this call is the
# documented escape hatch (see Pydantic docs: "Forward References").
GridTask.model_rebuild()


_AXIS_NAMES = ("accuracy", "safety", "cost", "sustainability", "side_effect_min", "pareto_utility")


class ParetoMatrix(BaseModel):
    model_config = _FROZEN
    schema_version: Literal["swarm_v8_pareto_matrix_v1"] = "swarm_v8_pareto_matrix_v1"
    axes: tuple[str, ...] = _AXIS_NAMES
    paths: tuple[Literal["A", "B", "third"], ...]
    scores: Mapping[Literal["A", "B", "third"], Mapping[str, float]]
    # __SLOT_P0A_ELIGIBILITY_2026_08_17__ Additive eligibility side-channel.
    # Default ``{}`` (empty mapping) preserves byte-identical construction
    # for every pre-P0A caller (``score_six_axis`` only populates this when
    # ``AGI_V8_SWARM_POD_ELIGIBILITY_ENABLED`` is on) — an empty mapping is
    # read by ``pareto_front`` as "no eligibility data supplied, run the
    # legacy unrestricted dominance search," so OFF/legacy round-trips are
    # untouched. When populated, ``pareto_front`` treats
    # ``eligible.get(path, True)`` as a hard pre-filter: a path mapped to
    # ``False`` can never enter the returned front (and therefore can never
    # be ``synthesizer.synthesize``'s ``chosen_path``) unless every path is
    # ineligible, in which case the legacy full-set fallback applies because
    # there is nothing better to prefer. Deliberately a ``bool`` map rather
    # than ``None``-valued axis scores: ``ParetoMatrix.scores`` stays
    # ``Mapping[str, float]`` so ``synthesizer.avg_score``'s
    # ``sum(scores.values())`` (out of this track's owned-file scope) never
    # has to learn to skip ``None``.
    eligible: Mapping[Literal["A", "B", "third"], bool] = Field(default_factory=dict)


class SplitBucket(BaseModel):
    model_config = _FROZEN
    schema_version: Literal["swarm_v8_split_bucket_v1"] = "swarm_v8_split_bucket_v1"
    jaccard: float
    threshold: float
    pod_a_top_weights: Mapping[str, float]
    pod_b_top_weights: Mapping[str, float]


class ThirdPathResult(BaseModel):
    model_config = _FROZEN
    schema_version: Literal["swarm_v8_third_path_v1"] = "swarm_v8_third_path_v1"
    gof_score: float
    selected_weights: Mapping[str, float]
    variance: float
    files: tuple[FileArtifact, ...]


class PrefixCacheKey(BaseModel):
    model_config = _FROZEN
    schema_version: Literal["swarm_v8_prefix_v1"] = "swarm_v8_prefix_v1"
    fingerprint: str
    cfg_hash: str


class MetaDecision(BaseModel):
    model_config = _FROZEN
    schema_version: Literal[
        "swarm_v8_meta_decision_v1",
        "swarm_v8_meta_decision_v2",
    ] = "swarm_v8_meta_decision_v1"
    generated_at: str
    pod_a_summary: dict
    pod_b_summary: dict
    third_path_summary: dict | None
    pareto_front: tuple[Literal["A", "B", "third"], ...]
    chosen_path: Literal["A", "B", "third", "split"]
    axis_matrix: ParetoMatrix
    file_patch: tuple[FileArtifact, ...]
    advisory: bool = True
    # __SLOT_W3A2__ 240-mode additive — default None preserves v1 round-trip.
    router_layer_summary: dict | None = None
    si_layer_summary: dict | None = None
    digestive_layer_summary: dict | None = None
    handoff_layer_summary: dict | None = None
    lane_layer_count: Mapping[str, int] | None = None
    # __SLOT_W7A5__ Did the streaming wave (W4.A4 + W7.A3) actually run?
    # True = ``_run_swarm_streaming`` consumed evidence stream and dispatched
    #        cross-cutting via the speculative meta queue.
    # False = streaming was attempted but fell back to the synchronous wave
    #         (e.g. lane_scaling_240 ON but SPECULATIVE_META OFF).
    # None  = streaming wave never engaged (not applicable; 192-mode path).
    # Default None preserves backwards compatibility for v1 callers.
    streaming_meta_used: bool | None = None


class TopologyConfigError(Exception):
    """Raised when topology parameters are internally inconsistent."""


class BudgetPruneException(Exception):
    """Raised when cumulative cost would exceed cfg.max_usd."""


# __SLOT_W3A2__ Subclass of BudgetPruneException so existing handlers keep
# catching it transparently; new cross-cutting paths (SI quarantine, handoff
# throttle) raise this subtype so they can be distinguished when needed.
class CrossCuttingPruneException(BudgetPruneException):
    """Raised when SI quarantine / handoff throttle prevents lane progress."""
