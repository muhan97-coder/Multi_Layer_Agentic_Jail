"""Public tick controls and lazy references to optional autonomous policy.

Parsing failures, stall admission and query identities remain inspectable when
no policy payload exists. Policy code is resolved only after a caller needs it.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from agi_v8_1.capabilities import PayloadPort, resolve_payload
from agi_v8_1.state.store import read_jsonl
from agi_v8_1.policy.fail_fast import (
    swallowed as _swallowed,
    safe_exception_type_name as _safe_exception_type_name,
)

WORK_ID_PREFIX = "wc_"
WORK_ENABLED = "AGI_V8_TICK_SELF_FEED_ENABLED"
STALL_ENABLED = "AGI_V8_STALL_VERDICT_ENABLED"
CAMPAIGN_ENABLED = "AGI_V8_GOAL_CAMPAIGN_TICK_FEED_ENABLED"


def work_enabled():
    return os.environ.get(WORK_ENABLED, "") in ("true", "1")  # tier: T9


def stall_enabled():
    return os.environ.get(STALL_ENABLED, "") in ("true", "1")  # tier: T7


def campaign_enabled():
    return os.environ.get(CAMPAIGN_ENABLED, "") in ("true", "1")  # tier: T2


def read_rows(path: Path, *, reader=read_jsonl) -> "tuple[list[dict[str, Any]] | None, str | None]":
    """관대한 원장 읽기 ⇒ ``(행들 | None, 결손 사유 | None)``.

    ⚠️ ``None`` = **못 읽었다**(빈 원장과 다른 사실이다).

    ``read_jsonl`` 은 깨진 줄에서 raise 한다. 여기 도달하는 경로는 전부 판정용이라
    원장 한 줄이 깨졌다고 **틱을 죽이면 안 된다**(``tick_runner._consumed_ids`` 가
    같은 파일에 대해 이미 관대한 이유). 대신 조용히 빈 목록으로 접지도 않는다 —
    그러면 깨진 젤이 **콜드스타트로 위장**해 급식이 계속된다.

    🔴 3라운드가 잡은 두 번째 모양: **유효 JSON 인데 객체가 아닌 줄**(``"str"`` ·
    ``null`` · ``[1,2]``). ``read_jsonl`` 은 통과시키고 호출부의 ``row.get(...)`` 이
    ``AttributeError`` 로 터진다 — "굶은 관측기는 crash 가 아니라 unknown 이어야
    한다"는 이 모듈의 명제를 정면으로 어긴다. ⇒ 그 줄은 **버리되 사실을 돌려준다.**
    돌려준 사유는 축(:func:`_ledger_health`)을 통해 판정으로, 그리고 원장 행으로 간다.
    """
    try:
        rows = reader(path)
    except Exception as exc:  # noqa: BLE001 — 깨진 원장이 틱을 죽이면 안 된다
        _swallowed(exc, site="runtime.work_feeder._read_rows", category="persist")
        return None, _safe_exception_type_name(exc)
    good = [r for r in rows if isinstance(r, dict)]
    if len(good) == len(rows):
        return good, None
    n = len(rows) - len(good)
    _swallowed(TypeError(f"{path.name}: 객체가 아닌 JSONL 행 {n}개를 버렸다"),
               site="runtime.work_feeder._read_rows", category="persist")
    return good, f"non_object_rows={n}"


class PolicyProxy:
    def __init__(self, kind: str):
        if kind not in ("work_feeder", "goal_campaign_feed", "tick_review"):
            raise ValueError("unknown tick policy")
        self._kind = kind

    def __getattr__(self, name):
        # Pure control helpers do not require an autonomy implementation.
        controls = {"work_feeder": {"_read_rows": read_rows, "enabled": work_enabled,
                                    "stall_enabled": stall_enabled, "ID_PREFIX": WORK_ID_PREFIX},
                    "goal_campaign_feed": {"enabled": campaign_enabled}}
        if name in controls.get(self._kind, {}):
            existing = sys.modules.get("agi_v8_1.runtime." + self._kind)
            if existing is not None:
                return getattr(existing, name)
            return controls[self._kind][name]
        loader = resolve_payload(PayloadPort(9,
            "agi_v8_1.runtime.autonomous_tick_payload", "load_policy"))
        return getattr(loader(self._kind), name)


work_feeder = PolicyProxy("work_feeder")
goal_campaign_feed = PolicyProxy("goal_campaign_feed")
tick_review = PolicyProxy("tick_review")
