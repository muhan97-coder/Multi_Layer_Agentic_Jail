# __SLOT_HALT_SENTINEL_2026_08_06__ 사람이 누른 정지의 **판정기**.
"""사람 정지 sentinel — 파일이 있으면 멈춘다.

## ⛔ 먼저: 이 모듈은 정지를 **보장하지 않는다**

정지를 보장하는 것은 레포 **밖**에서 root 로 도는 강제자(`agi-halt-enforcer`)
하나뿐이다. 이 모듈이 하는 일은 *"그 정지가 원장에서 ``unknown`` 으로 뭉개지지
않게 하는 것"* 이다. 둘을 섞어 적으면 그게 이 레포가 반복해 맞은 함정
(*"문서 ✅ armed = 게이트지 능력이 아님"*)이다.

왜 보장이 못 되나 — 적대검증이 실행으로 확인한 사실:

  - 모델 저작 파이썬이 workspace-owner uid 로 도는 잔여 표면은
    ``enforcement/command_executor.py`` 의 artifact run(샌드박스 미설정 시
    래퍼 없음)이다. ``si_lanes/verify_gate.py`` 의 옛 host pytest 표면은
    2026-08-17 Docker-only 격리(network/HOME/live gitdir 비노출, cgroup cap,
    실행 중 HALT poll)로 대체됐다.
  - 이 모듈도, 이 모듈을 부르는 호출부도 전부 workspace-owner 쓰기가능 트리 안이다.

⇒ **판정기와 호출부가 둘 다 공격자의 쓰기집합 안에 있으면 그건 킬스위치가
아니라 킬스위치의 그림이다.** 그래서 층을 가른다 — Layer 1(root 강제자)이
보증을, Layer 2(여기)가 원장 품질을 맡는다.

## 🔑 왜 소유권이지 위치가 아닌가

POSIX 에서 unlink 권한은 파일이 아니라 **부모 디렉터리의 쓰기권한**에 있다.
루프가 workspace-owner 로 도는 한, 그 계정이 쓸 수 있는 디렉터리에 둔 sentinel 은
어디에 두든 지워진다. 부모를 root 소유로 만드는 것이 유저 권한 임의코드에
대해 값싸게 structural 을 얻는 유일한 수단이다.

## 🔑 왜 ``lstat`` 인가 (``stat`` 이 아니라)

실측(2026-08-06), **끊긴 심링크**에 대해::

    Path(p).exists()  → False
    os.stat(p)        → FileNotFoundError(errno=2)
    os.lstat(p)       → 성공 (mode 0o120777)

⇒ ``stat``/``exists`` 로 판정하면, 미리 심어둔 **끊긴 심링크 하나로 무장 자체가
무력화된다** — 사람이 파일을 만들었다고 믿는데 판정기는 계속 "없다"고 답한다.
디렉터리 엔트리의 **존재**가 판정이지 그 대상의 존재가 아니다.

## 판정은 세 갈래이고, 두 개는 정지다

===============  ================================================
둘 다 ENOENT     ``halted=False``. **정상 부재** — ``swallowed`` 로
                 보내지 않는다(래칫이 정상 상태 인식을 결함으로 센다).
엔트리 존재      ``halted=True``. **내용은 판정에 관여하지 않는다** —
                 0바이트도, 깨진 JSON 도, ``{"halted": false}`` 도,
                 끊긴 심링크도, 디렉터리도 전부 정지다.
그 외 OSError    ``halted=True, readable=False``. 모름은 fail-closed.
===============  ================================================

내용이 판정을 **뒤집지 못하는** 비대칭이 핵심이다: 루프가 파일을 truncate 하거나
``{"halted": false}`` 를 써넣어 자기 스위치를 끄는 경로가 구조적으로 없다.

⚠️ ``readable=False`` 일 때는 **HUMAN 행을 쓰지 않는다.** 사람이 눌렀는지 모르는
상태에서 사람 개입을 원장에 주장하면 그게 오염이다.

## 비용 비대칭 (fail-closed 의 근거)

잘못 멈추면 사이클 하나를 늦게 시작할 뿐이고 $0 이다. 잘못 계속하면 사람이
멈추라고 한 루프가 돈을 쓰며 임의 파이썬을 실행한다.
"""
from __future__ import annotations

import os
import stat as _stat
from dataclasses import dataclass

from agi_v8_1.policy.fail_fast import CATEGORY_PERSIST, swallowed as _swallowed

__all__ = [
    "ACK_LEDGER",
    "HaltState",
    "HumanHalt",
    "SENTINEL_PRIVILEGED",
    "SENTINEL_UNPRIVILEGED",
    "TIER_PRIVILEGED",
    "TIER_UNPRIVILEGED",
    "is_halted",
    "raise_if_halted",
    "state",
]

#: root:root 0644, 부모 ``/etc/agi_v8`` 도 root:root 0755. **보증 tier.**
SENTINEL_PRIVILEGED = "/etc/agi_v8/HALT"
#: 프로비저닝 전에도 쓸 수 있는 약한 tier. ``SafeAutoApply(action='delete')`` 로
#: 지워질 수 있다 — 트레이가 이 자세를 "정지(약)" 로 **정직하게** 표시한다.
SENTINEL_UNPRIVILEGED = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "state", "si_jail", "tick", "STOP")
#: 확인 신호의 정본. root 소유이므로 루프가 "멈췄다"고 위조할 수 없다.
ACK_LEDGER = "/etc/agi_v8/ack.jsonl"

TIER_PRIVILEGED = "privileged"
TIER_UNPRIVILEGED = "unprivileged"

# ⚠️ 두 경로 모두 **절대경로 모듈 상수**다. ``state_dir`` 인자를 받지 않는다.
#
# 이유(실측): 캠페인은 ``first_run --state-dir <ep_dir>`` 로 에피소드마다 다른
# state_dir 을 준다. 약한 tier 를 ``<state_dir>/tick/STOP`` 으로 **상대** 해석하면
# 에피소드에서는 아무도 만들지 않는 경로가 되어, 돈을 가장 많이 쓰는 경로에서
# 스위치가 조용히 무효가 된다. 시그니처에 state_dir 을 안 넣어 그 오해를
# **문법적으로 불가능**하게 만든다.
_SENTINELS: tuple[tuple[str, str], ...] = (
    (TIER_PRIVILEGED, SENTINEL_PRIVILEGED),
    (TIER_UNPRIVILEGED, SENTINEL_UNPRIVILEGED),
)

_UNREADABLE = "__unreadable__"


class HumanHalt(BaseException):
    """사람이 루프를 멈췄다.

    ⚠️ ``Exception`` 이 아니라 **``BaseException``** 을 상속한다. 이 레포에는
    ``except Exception`` 삼킴이 도처에 있고(틱 디스패치 · 에피소드 사이클 ·
    캠페인 카드 …), 그중 **하나라도** 사람의 정지를 삼키면 원장에 *"에이전트가
    터졌다"*(``cycle_abort:HumanHalt``)로 남는다. 그건 사용자가 금지한 뭉개짐
    그 자체다. 삼킴을 뚫는 것이 이 클래스의 유일한 존재 이유다.
    """

    def __init__(self, halt_state: "HaltState") -> None:
        self.state = halt_state
        super().__init__(f"human halt: tier={halt_state.tier} "
                         f"sentinel={halt_state.sentinel} readable={halt_state.readable}")


@dataclass(frozen=True, slots=True)
class HaltState:
    """판정 결과. ⛔ ``halted`` 에 ``None`` 은 없다 — 판정은 **항상** 난다.

    "모름"은 ``halted=False`` 가 아니라 ``halted=True, readable=False`` 로
    표현된다. 모름을 계속-진행 쪽으로 접는 것이 이 프로젝트가 가장 비싸게 값을
    치른 실패 모양이고, 안전장치에서 그걸 반복하지 않는다.
    """

    halted: bool
    tier: str | None = None
    sentinel: str | None = None
    mtime_ns: int | None = None
    readable: bool = True
    error: str | None = None
    entry_kind: str | None = None

    @property
    def tier_is_guaranteed(self) -> bool:
        """이 정지가 **보증 tier**에서 왔나. 약한 tier 는 지워질 수 있다."""
        return self.tier == TIER_PRIVILEGED


def _kind(mode: int) -> str:
    if _stat.S_ISLNK(mode):
        return "symlink"
    if _stat.S_ISDIR(mode):
        return "dir"
    if _stat.S_ISREG(mode):
        return "file"
    return "other"


def _probe(path: str) -> tuple[str | None, int | None, str | None]:
    """``(entry_kind, mtime_ns, error)``. ``(None, None, None)`` = 정상 부재.

    ⛔ ``os.stat`` 을 쓰지 않는다 — 끊긴 심링크를 ENOENT 로 접어 무장을
    무력화한다(모듈 독스트링의 실측 참조).
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        # 정상 부재. **``swallowed`` 로 보내지 않는다** — 침묵 핸들러 래칫의
        # 바늘은 "정상 상태 인식"과 "실패 삼킴"을 구분하지 못하고, 그걸 세면
        # 루프가 도는 것만으로 래칫이 붉어진다.
        return (None, None, None)
    except OSError as exc:
        _swallowed(exc, site="runtime.halt_sentinel._probe", category=CATEGORY_PERSIST)
        return (_UNREADABLE, None, f"{type(exc).__name__}(errno={exc.errno})")
    return (_kind(st.st_mode), int(st.st_mtime_ns), None)


def state() -> HaltState:
    """지금 정지 상태인가. **OR 판정** — 하나라도 있으면 정지다.

    보증 tier 를 먼저 본다. 존재하면 그걸로 확정하고 약한 tier 는 프로브하지
    않는다(약한 쪽이 못 읽혀도 판정은 이미 났다).
    """
    for tier, path in _SENTINELS:
        kind, mtime_ns, err = _probe(path)
        if kind is None:
            continue                       # 이 tier 는 부재 — 다음 tier
        if kind == _UNREADABLE:
            # 모름 → 멈춘다. ⚠️ readable=False 이므로 호출부는 HUMAN 행을
            # 쓰지 않는다(사람이 눌렀는지 모르는 상태다).
            return HaltState(halted=True, tier=tier, sentinel=path,
                             readable=False, error=err)
        return HaltState(halted=True, tier=tier, sentinel=path,
                         mtime_ns=mtime_ns, entry_kind=kind)
    return HaltState(halted=False)


def is_halted() -> bool:
    """``state().halted`` 의 축약. 로그를 쓰지 않는 값싼 검사."""
    return state().halted


def raise_if_halted() -> None:
    """정지면 ``HumanHalt`` 를 던진다.

    사이클 안쪽 체크지점(K0/K4/K5)에서 쓴다 — 거기서는 반환값으로 알리는 것보다
    삼킴을 뚫고 올라가는 것이 옳다.
    """
    st = state()
    if st.halted:
        raise HumanHalt(st)
