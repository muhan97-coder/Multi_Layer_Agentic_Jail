"""agi_v8_1.observability — telemetry / metric framework.

Ported from agi_v7.1/agent_system/observability/. R27 ships:
- ``parse_pytest_failures`` (verbatim port of test_output_parser)
- ``MetricLog`` — in-memory metric emission (no network)
- ``ObservabilityDashboard`` data class (collect() = stub returning empty)
- ``recent_proposal_events`` stub (no filesystem scan unless explicit root)

Heavy operational scanners (full dashboard.build_dashboard, learning_pipeline,
semantic_place_map, ...) are not ported — they require live runtime sessions
and exceed the 500-LoC module budget. They remain in v7.1 and will be ported
in a later round via the approved-runtime path.

# R27: deferred for later round  (heavy operational scanners)

R27 invariants:
- env knobs OFF by default (AGI_V8_OBSERVABILITY_ENABLED=false)
- metric emission is in-memory only
- no subprocess/network at module top
"""

from agi_v8_1.observability.test_output_parser import parse_pytest_failures
from agi_v8_1.observability.metric_log import (
    MetricLog,
    MetricEvent,
    get_default_metric_log,
)
from agi_v8_1.observability.dashboard_stub import (
    ObservabilityDashboard,
    build_dashboard_stub,
    recent_proposal_events_stub,
)

__all__ = [
    "parse_pytest_failures",
    "MetricLog",
    "MetricEvent",
    "get_default_metric_log",
    "ObservabilityDashboard",
    "build_dashboard_stub",
    "recent_proposal_events_stub",
]
