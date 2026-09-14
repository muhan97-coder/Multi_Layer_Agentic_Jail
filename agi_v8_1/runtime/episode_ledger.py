# __SLOT_EPISODE_LEDGER_2026_08_03__ 에피소드 실지출 — 부모와 자식이 같은 정의로.
"""한 에피소드가 지금까지 실제로 쓴 돈. 젤 안 두 원장의 **합**이다.

왜 별도 모듈인가: 이 값을 두 곳이 필요로 한다.

* **부모**(``runtime.goal_campaign``) — 에피소드가 끝난 뒤 원장 한 행에 적을 때.
* **자식**(``runtime.first_run``) — 사이클마다 "예산을 다 썼나"를 물을 때.

정의가 갈리면 자식이 "아직 남았다"고 믿고 도는 동안 부모는 초과를 기록하는,
서로 다른 두 진실이 생긴다. 그리고 자식은 의도적으로 의존성이 가벼운 모듈이라
부모(무거운 채점/린트 import 사슬)를 끌어올 수 없다.

**strict**: garbled 원장은 ``ValueError`` 로 전파한다. $0 으로 읽으면 "안 썼다"와
"못 읽었다"가 같은 값이 되고, 돈에서 그 둘을 섞는 것이 이 프로젝트에서 가장
비싸게 값을 치른 실패 모양이다(모름≠0).
"""
from __future__ import annotations

from pathlib import Path

from agi_v8_1.state.store import read_jsonl

#: Executor는 젤 상대 경로다. Cross-model의 상대 경로는 override가 없을 때의
#: 기본값이며, 실제 파일 선택은 verifier writer와 같은 public resolver를 쓴다.
LEDGERS = (("runtime_logs/executor_log.jsonl", "cost_usd"),
           ("runtime_logs/cross_model_verify_spend.jsonl", "usd"))


def _strict_rows_sum(rows, key: str, where: str) -> float:
    total = 0.0
    for row in rows:
        value = row.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"지출 원장 {where} 의 {key} 부적격(모름≠0): {value!r}")
        total += float(value)
    return total


def strict_ledger_sum(path: Path, key: str) -> float:
    """원장 한 파일의 *key* 합. 파일 부재는 진짜 $0, 부적격 값은 ValueError."""
    # malformed 줄 = JSONDecodeError 전파(fail-closed)
    return _strict_rows_sum(read_jsonl(path), key, path.name)


def cross_model_ledger_sum(state_dir: str | Path) -> float:
    """Strict, transaction-attributed cross-model spend for one state root."""
    # Lazy import keeps this parent/child budget primitive light at import time.
    from agi_v8_1.verifier.cross_model_budget import (
        read_spend_rows,
        resolve_ledger_path,
        transaction_charge_rows,
    )

    path = resolve_ledger_path(state_dir)
    rows = read_spend_rows(path)
    charges = transaction_charge_rows(rows)
    return _strict_rows_sum(charges, "usd", path.name)


def episode_spend(state_dir: str | Path) -> float:
    """이 에피소드의 실지출 = executor_log + cross-model 원장 **양쪽** 합산."""
    root = Path(state_dir)
    (executor_rel, executor_key), (_cross_rel, cross_key) = LEDGERS
    if cross_key != "usd":
        raise ValueError("cross-model ledger key drift")
    return round(
        strict_ledger_sum(root / executor_rel, executor_key)
        + cross_model_ledger_sum(root),
        8,
    )


__all__ = [
    "LEDGERS",
    "cross_model_ledger_sum",
    "episode_spend",
    "strict_ledger_sum",
]
