# __SLOT_STALL_VERDICT_2026_08_09__ 루프가 **자기가 산 일을 실제로 끝냈는지** 묻는다.
"""자가급식 정체 판정 — 산 후보와 아직 열린 후보의 **집합 뺄셈**. ⛔ LLM 을 안 부른다.

## ⚠️ 이 술어의 증거 범위는 **(창-1) × 쿨다운**이다 — 숫자가 아니라 **계산식**이다

⛔ "46일 공회전을 잡는다"는 거짓이다. 그런데 2라운드가 그 자리에 적은 *정정문*도
두 번 틀렸다. 둘 다 기록한다 — 정정이 스스로 같은 병에 걸리는 것이 이 파일의
주제이기 때문이다:

1. **산술 오류.** "창 × 쿨다운(5 × 6h ≈ 30시간)"이라고 썼다. 창 5건이 걸치는
   **간격은 4개**다 ⇒ ``(WINDOW-1) × 쿨다운``.
2. **썩는 표기.** 그날의 라이브 수치("dispatched 6건", "1.87일")를 커밋된 소스에
   박았다. 다음 스캔이 한 건 사면 그 문장은 즉시 거짓이 된다 — 이 레포가 이미
   값을 치른 패턴이고, 이 판의 규율이 **금지하는** 바로 그것이다.

⇒ 계산식으로 적는다::

    설계상 증거 범위 = (WINDOW - 1) × 자가급식 쿨다운
                     = (5 - 1) × 6시간 = 24시간            ← 기본값 기준
    실제 창 폭       = consumed.jsonl 의 `wc_` dispatched 행 중
                       마지막 WINDOW 건의 (last.ts - first.ts)

    재측정(1줄, 무비용)::

      python3 -c "import json;t=[r['ts'] for r in map(json.loads,\\
        open('state/si_jail/tick/consumed.jsonl')) if r.get('reason')=='dispatched'\\
        and str(r.get('id','')).startswith('wc_')];\\
        print(len(t),(t[-1]-t[0])/86400,(t[-1]-t[-5])/3600)"

⚠️ **2026-08-09 09:56 KST 실측 시점**의 관측: ``wc_`` dispatched 7건 · 전체 폭
2.125일 · 창(5) 폭 24.33시간. 이 세 수는 그 시점의 *관측*이지 계약이 아니다 —
**계약은 위 계산식이고, 수는 매일 자란다.**

46일 공회전의 실체는 ``no_pending``(큐가 비어 있었다)이고, 이 술어가 재는 것은
**"루프가 자기가 산 일을 끝냈나"** 다 — 유용하지만 **다른 현상**이다. 46일 쪽을
잡으려면 *"급식도 디스패치도 없는 틱이 며칠째 연속인가"* 를 세는 별도 술어가
필요하다(이 모듈에는 없다. 그건 산 것이 0건일 때 발화해야 하므로 여기 규칙 5와
정면으로 반대 방향이다).

## 왜 판정자가 LLM 이 아닌가

"산 일이 끝났는가"의 답은 *젤 안*이 아니라 *젤 밖*에 있다. 젤 로그만 보면 사이클마다
``workspace/answers/<hash>_<cycle_id>.md`` 가 새로 생기고 상태가 ``consensus`` 다 —
**워크스페이스 지문은 항상 움직인다.** 그래서 ``pre!=post`` 를 진전으로 읽는 오라클은
지금 이 공회전에 대해 "진전 중"이라고 답한다. 같은 로그를 LLM 에게 보여줘도 LLM 은
같은 것을 본다. 판정자를 비싸게 만들어도 **입력이 그 사실을 안 담고 있다.**

진짜 신호는 젤 밖에 있다: 관측원(실트리 정적 스캔)이 여전히 같은 결함을 잡는가.
그건 판단이 아니라 집합 뺄셈이고, 유료 판정자가 기여할 게 없다.

## 왜 공짜인가

유일한 무거운 계산 ``work_candidates.build()`` (레포 전수 정적 스캔) 를 **자가급식이
이미 지불하고 있다** (``work_feeder.next_candidate``). 이 모듈은 그 산출 리스트를
**인자로 받는다** ⇒ 한계비용 0. provider 를 import 하지 않고 env 를 읽지 않는다
(``runtime/tick_review`` · ``runtime/episode_judge`` 의 "PURE LOGIC + 주입 seam" 선례).

⚠️ 그 스캔의 벽시계는 **상수가 아니다** — 수십 초 규모라는 것만 계약이다. 3라운드가
"81~92초"라고 적었는데 4라운드 실측이 56.4초와 59.7·60.5·61.0초였다: **범위조차 하루를
못 갔다.** ⛔ 소스에 초 단위를 박지 마라 — 재려면
``AGI_V8_STALL_LIVE_SCAN=1 python3 -m pytest tests/v8_1/test_progress_stall_2026_08_09.py -k test_i2 -s``.

## 🔑 치명적 실패 모양은 하나가 아니라 **둘**이다 (1라운드에서 하나만 막았다)

이 판정이 뒤집히는 방향은 언제나 같다: 열린 후보 목록이 **덜 나오면** 산 후보가
"사라진" 것처럼 보여 ``progressing`` 이 되고, 급식(=지출)이 재개된다. 목록이 덜
나오는 길은 두 갈래다.

``관측기가 소리 내며 죽는다``
    ``work_candidates`` 가 실패를 ``source:"meta"`` 인 ``tool-error-*`` 후보
    **하나로 대체해서** 뱉는다(그 파일의 두 except 절). 규칙 3이 그것만 막는다.

``관측기가 소리 없이 빈손으로 돌아온다``  🔴 **1라운드가 놓친 쪽**
    ``last_fired.ledger_firings`` 는 원장 파일이 없으면 **예외 없이**
    ``{"present": False}`` 를 돌려주고, ``work_candidates`` 는 그걸 조용히 건너뛴다
    ⇒ ``never-fired-*`` 후보가 통째로 0건. 예외도 ``meta`` 도 안 난다.
    실측: ``executor_log.jsonl`` 하나만 치워도 판정이 ``stalled`` → ``progressing``
    으로 뒤집혔고, 비차단 판정은 원장에 행을 안 써서 **흔적조차 안 남았다.**
    같은 모양이 ``vocab_map._parse`` 의 SyntaxError 스킵에도 있다.

⇒ 그래서 이 모듈은 후보 목록만 받지 않는다. **호출자가 "관측기가 자기 입력을
실제로 읽었는가"를 같이 신고해야 한다**(``sources``, 필수 인자). 입력이 하나라도
결손이면 판정은 ``progressing`` 이 아니라 ``unknown`` 이다. ⛔ 신고 자체가 없으면
그것도 ``unknown`` 이다 — "안 물어봤다"를 "건강하다"로 접는 것이 바로 그 fail-open 이다.

## 🔴 2라운드가 여기서 **또** 실패했다 — 축을 하나씩 때웠기 때문이다

2라운드는 위 신고 기제를 만들고 "관측기의 침묵이 진전으로 읽힌다 = 고쳤다"고
적었다. 실제로는 **파일 하나**(``executor_log.jsonl``)에만 축을 달았다. 판정의
**다른 팔**인 산 후보 원장(``<jail>/tick/consumed.jsonl``)에는 축이 아예 없어서:

```
consumed.jsonl 삭제/0바이트/회전   → bought=[] → 규칙 5(증거부족) → 비차단 →
                                     급식 계속 + **원장 행 0** (실측 3/3 재현)
```

⇒ 결론은 하나다: **입력을 하나씩 방어하면 다음 입력에서 같은 구멍이 난다.**
그래서 이 모듈의 뼈대는 규칙이 아니라 :data:`OBSERVER_INPUTS` — **관측기가 읽는
입력의 전수 목록**이다. 신고가 그 목록을 **덮지 못하면** 그것부터 ``unknown``
이다(:data:`REASON_SOURCES_INCOMPLETE`). 새 입력이 생겼는데 축을 안 달면 판정이
나오는 게 아니라 **판정이 멈춘다.**

## ⛔ 묻지 않는 것 / **안 잡는 것** (전칭 주장을 남기지 않는다)

- **완료 주장을 절대 안 한다.** 어휘에 ``satisfied``/``done`` 이 없다. 이 판정이
  구동할 수 있는 행위는 "급식한다 / 안 한다" 둘뿐이고, 사이클을 종료시키거나 작업을
  완료 표시할 권한이 **구조적으로 없다.** 이 레포는 "스스로 완료 선언"에 이미 데였다.
- VERIFY verdict / HALT reason 을 안 읽는다 — 라이브 틱 경로에서 영원히 0행이라
  넣으면 팬텀 게이트가 된다.
- "같은 task id 반복"을 안 본다 — 툼스톤이 이미 완벽히 막아 발화 0이 보장된다.
- 정체의 **정도**를 안 잰다. 창 안 해결 0건이냐 아니냐, bool 하나다.

🔴 **이 판정이 못 잡는 구간** (3라운드 적대검증이 실측한 것 + 이번 판의 한계):

``트리를 조금 덜 본 것``
    ``Path.rglob`` 은 읽을 수 없는 디렉터리를 **예외 없이** 건너뛴다. 예외도 파싱
    에러도 아니라 **그때 있던 축이 전부** ``True`` 인 채 후보만 줄어들고(⛔ 개수를
    숫자로 적지 않는다 — :data:`SOURCE_AXES` 가 정본이다), 그러면 산 후보가
    "해결됐다"로 읽힌다. 실측: 디렉터리 하나(``memory/``, 전체의 ~2%)만 잃어도
    ``stalled`` → ``progressing`` 으로 뒤집혔다.
    ⇒ 이번 판이 넣은 것은 :data:`SCALE_KEYS` **규모 비교**다(스캔한 .py 파일 수·
    바이트를 원장에 남기고 다음 스캔이 대조). 그래서 이제 **잡는다**: 규모가 줄어든
    스캔은 "해결됐다"를 credit 하지 못한다(규칙 6b).
    ⛔ **그래도 못 잡는 것**: ① 기준선이 없는 **첫 스캔** ② 파일 수·바이트가 그대로인
    채 내용만 바뀌어 후보가 사라지는 경우(예: 그 파일이 파싱은 되는데 어휘 선언이
    지워짐 — 이건 진짜 해결과 원리적으로 구분 불가) ③ 기준선이 창 밖으로 밀려난 뒤
    (``prev`` 는 최근 :data:`WINDOW` 개 스캔의 최고수위다 ⇒ 줄어든 트리는
    약 ``WINDOW × 쿨다운`` 뒤에 **새 기준선이 된다**). ③ 은 의도된 한계다 —
    합법적 파일 삭제와 은닉을 원리적으로 구분할 수 없으므로, 영구 차단은 정상
    레포 진화를 영구히 막는다. ⇒ 이 축은 **탐지·기록** 장치이지 강제 장치가 아니다.

``기준선 원장(tick_log)이 회전·절단되면``
    규모 기준선은 **새 상태 파일이 아니라 ``tick/tick_log.jsonl`` 의 행**에 산다
    (writer 를 안 늘린다는 규율). 그래서 그 원장을 지우거나 잘라내면 기준선이
    같이 사라지고, 다음 스캔은 "첫 스캔"과 구분되지 않아 **막지 않는다.**
    실측(굶기기 표, tick_log × absent/empty/shrunk): 셋 다 ``fed_self``.
    ⛔ 이건 버그가 아니라 **교환**이다: 별도 상태 파일을 만들면 이 구멍은 막히지만
    이 레포가 이미 값을 치른 다른 병(원장이 여러 개로 갈라져 아무도 대조 안 함)이
    생긴다. 대신 정직하게 적는다 — *"tick_log 를 회전시키는 것은 이 게이트의
    기억을 지우는 것"* 이고, 회전 도구를 만드는 사람이 이 줄을 봐야 한다.

    🔴 **4라운드가 같은 교환의 더 비싼 절반을 찾았다.** 그 한 파일에는 기준선만
    사는 게 아니라 **쿨다운 앵커**도 산다. 그래서 파일이 못 읽히거나 못 써지면
    "안 막는다"로 끝나지 않고 **매 틱 전수 스캔**이 된다 — 스스로 degraded 라고
    판정한 바로 그 상태에서 제일 비싸게 도는 모양이다.

    🔴 **5라운드: 4라운드의 수리는 절반이었고, 그 사실을 하니스가 가렸다.**
    4라운드는 "쓸 수 있나"를 **프로브**(실 writer 와 같은 플래그로 ``os.open`` 해보고
    닫기)로 물었다. 프로브는 ``state.store.atomic_append_jsonl`` 의 네 단계 중
    ③ 하나만 복제한다::

        ① parent.mkdir(parents=True, exist_ok=True)
        ② os.open(<path>.lock, O_CREAT|O_RDWR) + flock      ← 사이드카
        ③ os.open(path, O_WRONLY|O_CREAT|O_APPEND|O_NOFOLLOW)  ← 프로브가 본 것
        ④ _write_all(fd, line); fsync                        ← ENOSPC 가 나는 자리

    그리고 4라운드 하니스는 **한 프로세스로 144틱**을 돌아 프로세스 내 보조 앵커의
    덕을 봤다 — 라이브는 10분 cron 이 틱마다 새 프로세스인데. 적대검증이 같은
    ENOSPC 젤을 두 방식으로 재서 ``builds=4`` vs ``builds=144`` 로 갈리는 것을
    보였다: **하니스가 정확히 그 판의 사각을 안 보이게 만들었다.**

    ⇒ 5라운드는 묻지 않고 **쓴다**. 순서가 곧 방어다: 쿨다운 확인(읽기) → 시도 행
    ``self_feed_attempt`` 를 **먼저 append** → 성공한 틱만 전수 스캔. 실 writer 가
    검사이므로 복제도, 복제의 표류도 없다.

    실측(``tick_once`` 층 · 게이트 ON · **cron 등가**(틱마다 in-proc 상태 clear) ·
    144틱 = 10분 cron 하루치 · 4라운드 트리 → 5라운드 트리)::

        control                        builds   4 →   4    rows   4 → 8
        tick_log 부재/빈/객체아닌줄     builds   4 →   4    (정상 젤 — 예산 유지)
        tick_log 잘린 줄               builds   0 →   0    rows   1 → 1
        0444 · 0000 · 디렉터리          builds   0 →   0
        심링크(정상/깨진)               builds   0 →   0
        RO 부모 + lock 있음             builds   4 →   4    (writer 가 실제로 성공한다)
        RO 부모 + lock 없음             builds 144 →   0    🔴 4라운드가 못 잡던 것
        RO 부모 + 파일 없음             builds 144 →   0    🔴
        lock 경로가 디렉터리            builds 144 →   0    🔴
        ``tick/`` 가 파일               builds 144 →   0    🔴
        ENOSPC(_write_all 주입)         builds 144 →   0    🔴

    ⛔ **대가 둘을 정직하게 적는다.**

    1. 원장이 하루 4행 는다(쿨다운당 시도 행 하나). 그 행에는 ``reason``·``ts``·
       ``dispatched`` 뿐이다 — 아직 아무것도 모르는 시점이라 아는 척을 안 한다.
    2. **고아 앵커**: 시도 행이 앉은 뒤 스캔 전/중에 프로세스가 죽으면 판정 행 없이
       앵커만 남고, 다음 쿨다운까지 스캔이 억제된다. 과억제의 상한은 **쿨다운
       1회분**이고 방향은 안전하다(비싼 걸 덜 한다). 그리고 프로브 설계와 달리 이건
       **원장에 보인다** — ``query_report.self_feed_orphan`` 이 연속 개수까지 띄운다.

    ⛔ 여전히 원장에 안 뜨는 것 하나: **쓰기 자체가 실패한** 억제(위 표의 0 들).
    못 쓰는 그 파일이 원장이기 때문이고, 사실은 ``policy.fail_fast`` 채널
    (WARNING + census)로만 나간다.

``틱 상류에서 죽던 원장`` (4라운드에 **행**이 됐다)
    ``tick_runner.tick_once`` 는 인박스(``tick/queries.jsonl``)와 툼스톤
    (``tick/consumed.jsonl``)을 **급식보다 먼저** 읽는다. 그 두 파일에 *유효 JSON 인데
    객체가 아닌 줄*(``"str"``/``null``/``[1,2]``)이 있으면 3라운드까지는 판정에
    도달하기 전에 ``AttributeError`` 로 틱이 죽었다.

    ⛔ **깨진 줄을 버리는 것은 여전히 안 한다.** 툼스톤을 버리면 이미 소비된 질의가
    다시 디스패치된다(실지출 재과금) — 그 판단은 유효하고 그대로다. 바뀐 것은
    *crash 냐 행이냐*뿐이다: 게이트 ON 이면 디스패치 없이
    ``reason="ledger_non_object_row"`` 행 하나를 남기고 돌아온다(전이에서만 기록 —
    이 경로는 스캔을 지불하지 않으므로 억제해도 태울 CPU 가 없다).
    ⚠️ 잃는 것도 적는다: **프로세스 종료코드가 0** 이 되어 systemd 가 실패로 안 센다.
    그래서 ``policy.fail_fast.swallowed`` 도 같이 부른다(STRICT 면 그대로 raise).
    ⛔ 게이트 OFF 는 HEAD 그대로 터진다(``test_c1d_*``). 계약은
    ``test_r10_*`` / ``test_r10c_*``.

``게이트가 꺼져 있을 때``
    OFF 는 바이트 동일이 계약이라 이 판정이 **존재하지 않는다.** 위 결함들은 OFF
    에서 전부 그대로 살아 있다(HEAD 유래). 그건 이 모듈의 사각이 아니라 **범위**다.

``이 모듈 밖의 새 관측 입력``
    :data:`OBSERVER_INPUTS` 는 ``work_feeder`` 가 읽는 것을 전수로 담고, 그 대응을
    ``test_r8_*`` 가 AST 로 검사한다. 하지만 **다른 모듈**이 관측 입력을 새로 읽기
    시작하면 이 검사는 못 본다. 그건 원리적 한계이지 구현 누락이 아니다.

## saturating 카운터를 왜 안 쓰나

Magentic-One 의 ``n_stalls += 1 / -= 1`` 은 **턴 단위 bool 을 누적**해야 해서 상태가
필요하다. 우리 신호는 이미 누적량이다(산 것 vs 아직 열린 것). 창을 두면 회복이 공짜로
나온다 — 창 안에서 한 건이라도 해결되면 즉시 ``progressing``. ⇒ **새 상태 파일을
안 만든다.** 그리고 Magentic-One main 의 회귀(트리거한 카운터를 리셋 안 해서 매 턴
재계획)가 **구조적으로 재현 불가능**하다 — 리셋할 카운터가 없기 때문이다.
"""
from __future__ import annotations

from agi_v8_1.capabilities import PayloadPort, PayloadUnavailable, resolve_payload
from agi_v8_1.policy.fail_fast import record_critical_failure

from typing import Any, Iterable, Mapping, Sequence

SCHEMA_VERSION = "stall_verdict_v1"

#: 닫힌 4값. ⛔ 서로 절대 안 합쳐진다 — 특히 ``unknown`` 은 제3의 값이다.
#: "진전 있음"으로 접으면 공회전을 영원히 못 잡고, "정체"로 접으면 멀쩡한 사이클을
#: 죽인다. 그래서 **행위는 묶어도 기록은 절대 안 묶는다**(아래 BLOCKING 참조).
STALL_VERDICTS = ("progressing", "stalled", "unknown", "insufficient_evidence")

PROGRESSING = "progressing"
STALLED = "stalled"
UNKNOWN = "unknown"
INSUFFICIENT = "insufficient_evidence"

#: 자가급식을 **막는** 판정. 값은 4개인데 행위는 2개다 — 어느 쌍을 묶느냐가 이
#: 설계의 진짜 결정이고, fail-closed 쪽으로 묶는다:
#:   ``unknown → 급식한다``(관대) 면 스캐너가 고장난 채로 돈을 계속 태우고 그 상태가
#:   "정상 진전"으로 원장에 남는다 — **보이지 않는 공회전**을 그대로 재생산한다.
#:   ``unknown → 급식 안 한다`` 면 최악이 "루프가 조용해지고 원장에 한 줄 선다"이다.
#: 이 비대칭이 안전한 이유는 하나뿐이다: 이 게이트는 지출을 *줄이는* 방향으로만
#: 작동하고, 사람의 작업은 구조적으로 못 막는다(``no_pending`` 분기 안에만 살기 때문).
BLOCKING_VERDICTS = frozenset({STALLED, UNKNOWN})

#: 최근 산 후보 몇 건을 보나. 시간 의미는 ``(WINDOW-1) × 쿨다운`` = 기본 4 × 6시간
#: = **24시간**의 미해결이다(⛔ ``WINDOW × 쿨다운`` 이 아니다 — 5건이 걸치는 간격은 4개다). ⚠️ 라이브 25사이클 표본 **하나**에서 고른 값이다 — 오탐·미탐률 미측정.
WINDOW = 5
#: 이 밑이면 판정하지 않는다. ⛔ 이게 없으면 산 후보 0건인 새 젤에서 ``resolved==0``
#: 이 참이라 즉시 ``stalled`` ⇒ **한 번도 급식 못 하고 영원히 잠긴다.** 조건이 항상
#: 성립하는 게이트는 조건이 절대 성립 안 하는 게이트만큼 나쁘다.
MIN_EVIDENCE = 3

#: 🔑 두 knob 의 **상한**. 하한(0/음수)만 막으면 게이트가 knob 하나로 **조용히
#: 무력화**된다 — 실측: ``MIN_EVIDENCE=1000`` ⇒ 영구 ``insufficient_evidence`` ⇒
#: 급식 무제한, ``WINDOW=999999`` ⇒ 창이 전체 이력이 되어 옛날 해결 하나로 영구 면죄.
#: 값의 근거: 산 후보는 쿨다운(기본 6시간)당 최대 1건이므로 20 ≈ **5일치 이력**이고,
#: 그 너머는 "최근"이 최근을 뜻하지 않는다. 범위 밖은 **거부**하고(기본값으로 접고)
#: 호출자가 그 사실을 판정 행에 싣는다 — ⛔ 무력화가 원장에 안 보이면 상한이 없는 것과 같다.
KNOB_MAX = 20

#: 🔑 **창의 하한.** 상한만 막았더니 반대 끝에 같은 병이 남아 있었다(4라운드 실측):
#: ``WINDOW=1`` 은 정합 범위 안인데 이 모듈 자신의 계산식으로 증거 범위가
#: ``(1-1) × 쿨다운 = 0시간`` 이다 — 가장 최근에 산 후보 **하나**만 해결되면 무조건
#: ``progressing`` 이 되어, 창이라는 개념 자체가 사라진다. 실측::
#:
#:     WINDOW=5 → window_ids=[b1..b5] (증거범위 24h)
#:     WINDOW=2 → window_ids=[b4,b5]  (증거범위  6h)
#:     WINDOW=1 → window_ids=[b5]     (증거범위  0h)   ← 무력화
#:
#: ⇒ **창에만** 하한 2를 건다. ⛔ ``MIN_EVIDENCE`` 에는 안 건다 — 거기서 1은
#: "산 게 하나만 있어도 판정한다"는 **정상적인 뜻**이고 무력화가 아니다.
#: ⚠️ 하한 2도 증거 범위는 쿨다운 하나(기본 6시간)뿐이다. 이건 "안전한 값"이 아니라
#: **0시간이 아닌 최소값**이고, 범위 밖 거부와 똑같이 판정 행에 기록된다.
WINDOW_MIN = 2

#: ``work_candidates`` 가 **자기 고장을 신고할 때** 쓰는 source 값.
META_SOURCE = "meta"

# ─────────────── 🔑 관측 입력 전수 목록 (이 판의 뼈대) ───────────────

#: 입력이 판정에 대해 갖는 **권한**. 셋을 안 나누면 둘 중 하나가 된다:
#: 전부 차단 ⇒ 젤에 ``apply_chain.jsonl`` 이 아직 없는 정상 상태가 급식을 막는다.
#: 전부 비차단 ⇒ 축이 장식이 된다.
ROLE_VERDICT = "verdict"          #: 못 읽었으면 **항상** 차단(unknown).
ROLE_RESOLUTION = "resolution"    #: "해결됐다"를 credit 할 때만 차단(규칙 6b).
ROLE_INSTRUMENT = "instrument"    #: 절대 차단 안 한다. 행에 값만 남는다.
SOURCE_ROLES = (ROLE_VERDICT, ROLE_RESOLUTION, ROLE_INSTRUMENT)

#: 🔑 **관측기(그리고 이 판정 경로)가 읽는 입력의 전수 목록.**
#:
#: ⚠️ 2라운드는 이 목록 없이 축을 하나씩 달다가 다른 팔에 같은 구멍을 남겼다
#: (모듈 헤더 "2라운드가 여기서 또 실패했다" 참조). 목록이 **먼저**여야 하는
#: 이유는 하나다: 신고가 이 목록을 못 덮으면 :func:`stall_verdict` 가
#: :data:`REASON_SOURCES_INCOMPLETE` 로 **판정을 거부한다** ⇒ 새 입력을 읽는
#: 코드를 추가하고 축을 안 달면, 판정이 관대해지는 게 아니라 **멈춘다.**
#:
#: ⛔ 이 목록은 "우리가 읽는다고 믿는 것"이 아니라 **읽는 코드가 있는 것**이다.
#: ``tests/v8_1/test_progress_stall_2026_08_09.py::test_r8_*`` 가 ``work_feeder``
#: 의 원장 경로 상수를 AST 로 훑어 이 목록과 대조한다 — 사람이 잊는 것을 막는 유일한
#: 장치다(그 테스트도 **이 모듈 밖의** 새 리더는 못 본다. 그건 아래 한계로 적는다).
OBSERVER_INPUTS: "tuple[dict[str, str], ...]" = (
    {"axis": "bought_ledger", "role": ROLE_VERDICT,
     "reads": "<jail>/tick/consumed.jsonl",
     "why": "판정의 한 팔(산 후보). 부재·0바이트·회전이 전부 '콜드스타트'로 위장한다"},
    {"axis": "inbox_queue", "role": ROLE_VERDICT,
     "reads": "<jail>/tick/queries.jsonl",
     "why": "이미 큐에 있는 일감. 못 읽으면 같은 후보를 두 번 사서 돈을 태운다"},
    {"axis": "tick_log", "role": ROLE_VERDICT,
     "reads": "<jail>/tick/tick_log.jsonl",
     "why": "쿨다운 앵커 + 규모 기준선. 못 읽으면 전수 스캔이 매 틱 돈다"},
    {"axis": "executor_log", "role": ROLE_VERDICT,
     "reads": "last_fired.build 의 ledger 기본값(=state/runtime_logs/executor_log.jsonl)",
     "why": "판정의 다른 팔(never-fired 후보). 없으면 예외 없이 통째로 0건이 된다"},
    {"axis": "last_fired_ast", "role": ROLE_VERDICT,
     "reads": "last_fired.UNPARSED (관측기 자기신고)",
     "why": "AST 파싱을 건너뛴 소스 파일이 있으면 그 파일의 후보가 통째로 빠진다"},
    {"axis": "vocab_map_ast", "role": ROLE_VERDICT,
     "reads": "vocab_map.UNPARSED (관측기 자기신고)", "why": "위와 같다"},
    {"axis": "observer_quiet", "role": ROLE_VERDICT,
     "reads": "policy.fail_fast.census() 의 관측기 choke point 계수 델타",
     "why": "UNPARSED 사전이 안 잡는 것(원장 안의 깨진 JSON 줄)을 잡는다"},
    {"axis": "scan_scale", "role": ROLE_RESOLUTION,
     "reads": "관측기 루트의 .py 파일 수·바이트 (직전 스캔들의 최고수위와 대조)",
     "why": "rglob 은 못 읽는 디렉터리를 예외 없이 건너뛴다 — '덜 봤다'가 '고쳤다'로 읽힌다"},
    {"axis": "apply_chain", "role": ROLE_INSTRUMENT,
     "reads": "<jail>/apply_chain.jsonl", "why": "젤축 계측 전용. 새 젤엔 정상적으로 없다"},
    {"axis": "cycle_log", "role": ROLE_INSTRUMENT,
     "reads": "<jail>/cycle_log.jsonl", "why": "젤축 계측 전용. 새 젤엔 정상적으로 없다"},
)

#: 신고가 **반드시 덮어야 하는** 축 이름들(= 위 목록 전부, 순서 보존).
SOURCE_AXES: "tuple[str, ...]" = tuple(d["axis"] for d in OBSERVER_INPUTS)
_ROLE_OF: "dict[str, str]" = {d["axis"]: d["role"] for d in OBSERVER_INPUTS}

#: 규모 비교에 쓰는 수치 키. ⛔ ``None``(못 쟀다)과 ``0``(정말 0)을 절대 안 섞는다.
SCALE_KEYS = ("bought_n", "py_files", "py_bytes")
#: 어느 수치가 어느 축을 지키나. 한 축이 여러 수치를 볼 수 있다(하나라도 줄면 결손).
SCALE_AXIS_KEYS: "dict[str, tuple[str, ...]]" = {
    "bought_ledger": ("bought_n",),
    "scan_scale": ("py_files", "py_bytes"),
}


def axis_role(axis: str) -> str:
    """축 이름 → 권한. ⛔ 목록에 없는 축은 **가장 센 쪽**(verdict)으로 친다.

    모르는 축을 관대하게 접으면, 오타 하나가 그 축의 차단력을 조용히 없앤다.
    """
    return _ROLE_OF.get(axis, ROLE_VERDICT)


REASON_BOUGHT_UNREADABLE = "bought_ledger_unreadable"
REASON_BUILD_FAILED = "candidate_build_failed"
REASON_SCHEMA_INVALID = "candidate_schema_invalid"
REASON_TOOL_ERROR = "candidate_tool_error"
#: 관측기가 자기 입력을 못 읽었다(파일 부재·빈 파일·깨진 줄·파싱 스킵).
REASON_SOURCES_DEGRADED = "observer_sources_degraded"
#: 호출자가 입력 소스 신고를 아예 안 했다. ⛔ "안 물어봤다"는 "건강하다"가 아니다.
REASON_SOURCES_UNREPORTED = "observer_sources_unreported"
#: 신고는 왔는데 :data:`OBSERVER_INPUTS` 를 **덮지 못했다**(축 하나가 빠졌다).
#: ⛔ 이게 없으면 "새 입력을 읽으면서 축을 안 다는 것"이 무증상으로 통과한다 —
#: 정확히 2라운드가 저지른 실패다.
REASON_SOURCES_INCOMPLETE = "observer_sources_incomplete"
REASON_INSUFFICIENT = "bought_lt_min_evidence"
REASON_WINDOW_ZERO = "window_resolved_zero"
REASON_WINDOW_SOME = "window_resolved_some"
#: 창 안에서 후보가 사라지긴 했는데 **그 스캔이 직전보다 트리를 덜 봤다.**
#: ⇒ "고쳐졌다"와 "덜 봤다"가 구분 안 되므로 진전이 아니라 모름이다.
REASON_RESOLUTION_UNVERIFIABLE = "resolution_scan_shrank"

#: 젤 안에서 재는 "움직였나" 축들. ⛔ **판정을 구동하지 않는다** — 계측 전용이다.
#: 셋 다 이 공회전에 대해 "진전 중"이라고 답하는 것이 확인됐기 때문이다.
GEL_AXIS_KEYS = ("workspace_moved", "outcome_fp_novel", "objective_template_novel")

__all__ = [
    "SCHEMA_VERSION", "STALL_VERDICTS", "BLOCKING_VERDICTS", "WINDOW", "MIN_EVIDENCE",
    "KNOB_MAX", "WINDOW_MIN",
    "PROGRESSING", "STALLED", "UNKNOWN", "INSUFFICIENT", "META_SOURCE",
    "GEL_AXIS_KEYS", "stall_verdict", "dedup_keep_last", "gel_axes_from_rows",
    "source_summary", "candidate_shape_ok",
    "OBSERVER_INPUTS", "SOURCE_AXES", "SOURCE_ROLES", "ROLE_VERDICT",
    "ROLE_RESOLUTION", "ROLE_INSTRUMENT", "axis_role", "SCALE_KEYS",
    "SCALE_AXIS_KEYS", "scale_axes", "merge_scale", "unreported_axes", "role_gaps",
]

# ─────────────────────────── 젤 축 계측 (판정 비구동) ───────────────────────────

_OUTCOME_FP_FIELDS = ("status", "axes_aligned", "axis_evidence_total",
                      "stub_inputs", "policy_drift_keys")


def _norm_objective(text: str) -> str:
    """목표 문장 → 템플릿 지문. 인용 심볼·파일위치·숫자를 지운다.

    ⚠️ **휴리스틱이다.** 다른 정규화를 쓰면 발화 지점이 달라진다. 그래서 이 축은
    판정을 구동하지 않고 계측 칸에만 앉는다.
    """
    import re
    t = re.sub(r"`[^`]*`", "`X`", text)
    t = re.sub(r"\([^()]*\.py:\d+\)", "(F:N)", t)
    t = re.sub(r"[\d,]*\d", "N", t)
    return " ".join(t.split())


def _last_novel(rows: "Sequence[Mapping[str, Any]]", key) -> bool | None:
    """마지막 행의 지문이 이 원장에서 **처음 나온 것**인가. 모르면 None.

    ``key(row)`` 가 ``None`` 을 내면 그 행은 지문을 못 만든 것이라 셈에서 뺀다.
    마지막 행의 지문 자체가 ``None`` 이면 판단 불가 ⇒ ``None``.
    """
    fps = [key(r) for r in rows if isinstance(r, Mapping)]
    fps = [f for f in fps if f is not None]
    if not fps:
        return None
    return fps.count(fps[-1]) == 1


def _mapping_rows(rows: Any) -> "list[Mapping[str, Any]]":
    """원장 행 목록에서 ``Mapping`` 원소만. 못 읽었으면(``None``) 빈 목록.

    ⛔ ``rows or ()`` 관용구를 안 쓴다. 여기서는 두 입력(``None`` = 못 읽었다,
    ``[]`` = 비었다)이 결국 같은 답(축 ``None`` = 모름)으로 가므로 결과는 같지만,
    이 레포에서 그 관용구는 *"못 읽었다를 비었다로 접는 자리"* 의 표식이고 다음
    사람이 그대로 복사한다. 관용구 자체를 남기지 않는 것이 이 함수의 존재 이유다.
    """
    if isinstance(rows, (str, bytes, Mapping)) or not isinstance(rows, Iterable):
        return []
    return [r for r in rows if isinstance(r, Mapping)]


def gel_axes_from_rows(*, apply_chain_rows: "Sequence[Mapping[str, Any]] | None" = None,
                       cycle_log_rows: "Sequence[Mapping[str, Any]] | None" = None,
                       tick_log_rows: "Sequence[Mapping[str, Any]] | None" = None,
                       ) -> dict[str, bool | None]:
    """젤 안에서 재는 "움직였나" 축 3종. ⛔ **판정을 구동하지 않는다.**

    셋 다 이 공회전에 대해 "진전 중"이라고 답하는 것이 라이브 리플레이에서 확인됐다 —
    사이클마다 새 answer md 를 쓰기 때문에 워크스페이스 지문은 **항상 움직인다.**
    그래서 계측으로만 남긴다. 못 재면 ``None`` 이다(⛔ False 로 접지 않는다).
    """
    ax: dict[str, bool | None] = {k: None for k in GEL_AXIS_KEYS}

    ac = _mapping_rows(apply_chain_rows)
    if ac:
        pre, post = ac[-1].get("pre_apply_hash"), ac[-1].get("post_apply_hash")
        if isinstance(pre, str) and isinstance(post, str):
            ax["workspace_moved"] = pre != post

    ax["outcome_fp_novel"] = _last_novel(
        _mapping_rows(cycle_log_rows),
        lambda r: (repr([r.get(f) for f in _OUTCOME_FP_FIELDS])
                   if any(f in r for f in _OUTCOME_FP_FIELDS) else None))

    ax["objective_template_novel"] = _last_novel(
        _mapping_rows(tick_log_rows),
        lambda r: (_norm_objective(r["objective"])
                   if isinstance(r.get("objective"), str) else None))
    return ax


def dedup_keep_last(ids: Iterable[Any]) -> list[str]:
    """도착 순서를 보존하되 중복은 **마지막 등장 위치**로 접는다.

    창은 "최근"을 재므로, 같은 후보를 두 번 샀다면 최근 구매가 그 후보의 자리다.
    (툼스톤이 재디스패치를 막으므로 실제로는 중복이 안 나오지만, 그 불변식이 깨져도
    창의 의미가 흔들리지 않게 여기서 정의를 박아둔다.)
    """
    out: list[str] = []
    for raw in ids:
        s = str(raw)
        if s in out:
            out.remove(s)
        out.append(s)
    return out


def _gel_says_progress(axes: Mapping[str, Any]) -> bool | None:
    """젤 축들이 "움직였다"고 말하는가. **모르면 None** — False 로 접지 않는다.

    하나라도 True 면 True(움직인 증거가 있다). 셋 다 **알려진 채로** False 여야
    False. 그 밖(일부/전부 미상)은 None 이다.
    """
    vals = [axes.get(k) for k in GEL_AXIS_KEYS]
    if any(v is True for v in vals):
        return True
    if all(v is False for v in vals):
        return False
    return None


def _axes_block(resolution_says_stalled: bool | None,
                gel_axes: "Mapping[str, Any] | None") -> dict[str, Any]:
    """무료 계측 블록 — **유료 LLM 층의 진입조건을 재는 자리.**

    ``axes_disagree`` 가 계속 True 면 젤축이 쓸모없다는 증거가 쌓이는 것이고,
    **해결축이 틀렸다고 사람이 판정한 사례가 나오는 날**이 유료 판정자의 진입조건이다.
    ⇒ 비싼 것을 짓는 대신 **비싼 것이 필요해질 조건을 계측한다.**
    """
    src: Mapping[str, Any] = gel_axes if isinstance(gel_axes, Mapping) else {}
    out: dict[str, Any] = {}
    for k in GEL_AXIS_KEYS:
        v = src.get(k)
        # ⛔ bool 만 받는다. 미상·이상값은 None(모름)이지 False 가 아니다.
        out[k] = v if isinstance(v, bool) else None
    out["resolution_says_stalled"] = resolution_says_stalled
    gel = _gel_says_progress(out)
    # 젤축이 "진전" 인데 해결축이 "정체" 면 불일치. 한쪽이라도 모르면 **비교 불가**
    # 이므로 None 이다 — ⛔ False("일치한다")로 접으면 모름이 합의로 읽힌다.
    out["axes_disagree"] = (
        None if gel is None or resolution_says_stalled is None
        else gel is resolution_says_stalled
    )
    return out


def candidate_shape_ok(c: Any) -> bool:
    """후보 하나가 **비교에 쓸 수 있는 모양**인가.

    ⛔ 이 검사가 없으면 스키마가 조금만 흔들려도(원소가 dict 가 아니거나 ``id`` 가
    없거나) 그 원소가 열린 id 집합에서 빠져 산 후보가 "해결됐다"로 읽힌다 —
    ``or ()`` 널 접기와 **같은 뿌리의 fail-open** 이다. 실측: ``["oops"]`` 한 줄이
    창 5건을 전부 resolved 로 만들었다.
    """
    if not isinstance(c, Mapping):
        return False
    cid, src = c.get("id"), c.get("source")
    return isinstance(cid, str) and bool(cid) and isinstance(src, str) and bool(src)


def _scale_of(scale: Any, key: str) -> "int | None":
    """규모 수치 하나. ⛔ ``bool``/문자열/음수는 **모름**이지 0 이 아니다."""
    if not isinstance(scale, Mapping):
        return None
    v = scale.get(key)
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        return None
    return v


def merge_scale(scales: "Iterable[Any]") -> "dict[str, int | None]":
    """여러 스캔 규모의 **최고수위**(축별 max). 못 잰 것은 ``None`` 으로 남는다.

    🔑 기준선을 "직전 한 번"이 아니라 **최근 여러 스캔의 최고수위**로 잡는 이유:
    직전 하나만 보면 공격자가 한 쿨다운만 기다리면 줄어든 트리가 곧 새 기준선이
    된다(6시간). 최고수위면 그 유예가 ``창 × 쿨다운`` 으로 늘어난다. ⛔ 그래도
    **영구는 아니다** — 합법적 파일 삭제와 은닉을 원리적으로 구분할 수 없어서,
    영구 차단은 정상 레포 진화를 영구히 막는다(모듈 헤더 '안 잡는 것' ③ 참조).
    """
    out: "dict[str, int | None]" = {k: None for k in SCALE_KEYS}
    for s in scales:
        for k in SCALE_KEYS:
            v = _scale_of(s, k)
            if v is None:
                continue
            cur = out[k]
            out[k] = v if cur is None else max(cur, v)
    return out


def scale_axes(now: Any, prev: Any) -> "dict[str, bool | None]":
    """규모 비교 → ``{축: True(안 줄었다) / False(줄었다) / None(모름)}``.

    ⚠️ 기준선(``prev``)이 없으면 **모름이 아니라 True** 다. 첫 스캔에는 비교할
    대상이 자체가 없고, 거기서 ``None``(=결손)을 내면 새 젤이 첫 판정부터 막힌다 —
    "조건이 항상 성립하는 게이트는 조건이 절대 성립 안 하는 게이트만큼 나쁘다"
    (:data:`MIN_EVIDENCE` 주석과 같은 논리). ⛔ 그 대신 **첫 스캔은 못 잡는다**는
    사실을 모듈 헤더 '안 잡는 것' ① 에 적었다.

    지금 값을 못 쟀는데(``None``) 기준선은 있는 경우는 ``None``(모름) 이다 —
    ⛔ 여기서 True 로 접으면 "재는 코드가 죽은 것"이 "건강하다"가 된다.

    ## 임계가 "어떤 감소든"인 이유 (4라운드에 재고했고 **안 바꿨다**)

    적대검증이 옳게 지적했다: ``.py`` 하나 삭제도, 한 파일 **4바이트** 축소도
    똑같이 :data:`REASON_RESOLUTION_UNVERIFIABLE` 로 급식을 막는다. 그리고 창 안
    후보의 다수 class 인 ``vocab-hole-*`` 의 표준 해결이 *"어휘를 줄여라"* = **바이트가
    준다** 이므로, 이 게이트의 성공 조건과 오탐 조건이 겹칠 수 있다.

    검토한 완화 셋과 기각 사유::

        허용 오차(예: 1% 이내 감소는 통과)  ⇒ 딱 그만큼의 은닉이 무료가 된다.
                                              rglob 이 건너뛴 디렉터리 하나가 1% 미만인
                                              모양이 실제로 있었다(memory/ ≈ 2%).
        py_files 만 보고 바이트는 무시       ⇒ 파일 수 그대로 내용만 비우는 은닉을 놓친다.
        기준선을 직전 하나로              ⇒ 한 쿨다운만 기다리면 줄어든 트리가 새 기준선.
                                              (:func:`merge_scale` 이 이미 기각한 것)

    ⇒ **임계는 그대로 두고 두 가지를 바꿨다**: ① 배너가 이걸 "관측기가 못 읽은 입력"
    이라고 부르던 **거짓말**을 고쳤다(읽기 실패와 규모 축소는 다른 칸이다) ②
    해제 조건을 명시했다 — 합법적 축소면 **아무것도 안 해도** 줄어든 규모가 새
    기준선이 되는 다음 창에서 풀린다(최대 ``WINDOW × 쿨다운``).

    ⚠️ 오탐 기저율(2026-08-09, 관측기와 **같은 필터**로 git 이력 재계산): 최근 300커밋
    = **299 쌍** 중 축소 **1건(0.33%)** — ``1bfad771 "chore: drop 5 unused exception
    bindings"``(-44 B). 4라운드가 적대검증과 **독립으로 다시 계산해 같은 값**을 얻었다.
    ⛔ 이건 **커밋 입도**의 수다 — 실제 스캔은 여러 판이 동시에 편집 중인
    **워킹트리**를 본다. 그 입도의 오탐율은 3·4라운드 모두 **못 쟀다.**
    """
    out: "dict[str, bool | None]" = {}
    for axis, keys in SCALE_AXIS_KEYS.items():
        verdict: "bool | None" = True
        for k in keys:
            cur, base = _scale_of(now, k), _scale_of(prev, k)
            if base is None:
                continue                     # 기준선 없음 = 비교 대상 없음
            if cur is None:
                verdict = None if verdict is not False else False
                continue
            if cur < base:
                verdict = False
        out[axis] = verdict
    return out


def unreported_axes(sources: Any) -> "list[str]":
    """:data:`OBSERVER_INPUTS` 중 신고에서 **빠진** 축들. 없으면 ``[]``.

    ⛔ 값이 ``False``/``None`` 인 것은 여기 안 센다 — 그건 "신고했는데 결손"이고
    이건 "신고 자체가 없다"다. 두 사실은 처방이 다르다(전자는 입력을 되살려라,
    후자는 **축을 다는 것을 잊었다**).
    """
    if not isinstance(sources, Mapping):
        return list(SOURCE_AXES)
    return [a for a in SOURCE_AXES if a not in sources]


def role_gaps(sources: Any, role: str) -> "list[str]":
    """이 권한을 가진 축 중 **결손인 것**들. ⛔ ``True`` 가 아닌 값은 전부 결손이다.

    ⚠️ 이 함수가 :func:`source_summary` 의 ``missing`` 과 **다른 이유**: 저건 사람이
    읽을 목록(원장·배너용)이고 이건 **차단 결정**이다. 하나로 합치면 ``verdict``
    권한과 ``resolution`` 권한이 같은 시점에 같은 힘으로 막게 되고, 그러면
    :data:`ROLE_RESOLUTION` 이라는 구분 자체가 사라진다(실제로 초안이 그랬다).
    """
    if not isinstance(sources, Mapping):
        return []
    return sorted(str(k) for k, v in sources.items()
                  if v is not True and axis_role(str(k)) == role)


def source_summary(sources: Any) -> "tuple[list[str] | None, list[str] | None]":
    """``(읽힌 소스, 결손 소스)``. 신고 자체가 없으면 ``(None, None)``.

    ``sources`` 는 ``{소스 이름: True(읽었다) / False(결손) / None(확인 불가)}`` 다.
    ⛔ ``True`` 가 아닌 것은 **전부 결손 쪽**이다 — "확인 불가"를 "건강하다"로 접는
    것이 이 모듈이 막으려는 fail-open 그 자체이기 때문이다.

    ⚠️ ``missing`` 에는 :data:`ROLE_INSTRUMENT` 축을 **안 싣는다.** 그 축들은 새 젤에
    정상적으로 없고(``apply_chain.jsonl``), 그걸 결손 목록에 실으면 배너가 매번
    "관측기가 못 읽은 입력" 을 외쳐 진짜 결손이 잡음에 묻힌다. 전 축의 원값은
    판정 결과의 ``sources_report`` 로 **따로 전부** 실린다(감사 가능성은 유지).
    """
    if not isinstance(sources, Mapping) or not sources:
        return None, None
    read = sorted(str(k) for k, v in sources.items() if v is True)
    missing = sorted(str(k) for k, v in sources.items()
                     if v is not True and axis_role(str(k)) != ROLE_INSTRUMENT)
    return read, missing


def stall_verdict(*, bought_ids: "Sequence[Any] | None",
                  open_candidates: "Sequence[Mapping[str, Any]] | None",
                  sources: "Mapping[str, Any] | None",
                  window: int = WINDOW, min_evidence: int = MIN_EVIDENCE,
                  gel_axes: "Mapping[str, Any] | None" = None) -> dict[str, Any]:
    """루프가 자기가 산 일을 끝내고 있는가.

    :param bought_ids: ``consumed.jsonl`` 의 ``reason=="dispatched"`` 이고 ``wc_``
        접두인 id 들에서 접두를 제거한 것. **도착 순서 보존.** 호출자가 준다.
    :param open_candidates: ``work_candidates.build()["candidates"]`` 원본 리스트.
        ``None`` = 산출 자체가 실패했다(빈 목록과 **다른 사실**이다). 호출자가 준다.
    :param sources: 🔑 **필수.** 관측기가 자기 입력을 실제로 읽었는지의 신고
        (``{이름: True/False/None}``). ⛔ 기본값을 주지 않는 것이 계약이다 —
        기본값이 있으면 호출자가 신고를 빼먹어도 판정이 나오고, 그게 곧
        "관측기의 침묵이 진전으로 읽힌다"는 그 결함이다. 모르면 ``None`` 을 넘겨라
        (그러면 ``unknown`` 이 나온다. 그것이 정직한 답이다).

    판정 순서가 곧 계약이다 (⛔ 이 순서를 바꾸지 마라)::

        0. bought_ids is None             → unknown  bought_ledger_unreadable
        1. open_candidates is None        → unknown  candidate_build_failed
        2. 원소 모양이 깨졌다             → unknown  candidate_schema_invalid
        3. any(source == "meta")          → unknown  candidate_tool_error
        4a. 입력 소스 신고 자체가 없다    → unknown  observer_sources_unreported
        4b. 신고가 전수 목록을 못 덮는다  → unknown  observer_sources_incomplete
        4c. verdict 권한 축이 결손        → unknown  observer_sources_degraded
        5. len(bought) < min_evidence     → insufficient_evidence
        6. resolved_in_window == 0        → stalled
        6b. 해결은 있는데 스캔이 줄었다   → unknown  resolution_scan_shrank
        7. otherwise                      → progressing

    ⚠️ 4가 5보다 **앞**인 것은 의도다. 관측기가 눈이 먼 상태에서는 콜드스타트조차
    확인할 수 없다 ⇒ 급식을 막는다. 대가는 유한하다(루프가 조용해지고 원장에 한 줄
    선다). 반대로 접으면 눈먼 관측기가 계속 돈을 태우고 그게 "정상"으로 기록된다.

    ⚠️ 6b 가 6 **뒤**인 것도 의도다. 규모가 줄어도 해결이 0건이면 판정은 어차피
    ``stalled``(차단)이고, 그 경우 굳이 ``unknown`` 으로 바꾸면 운영자가 읽을 사유가
    나빠진다. 규모 축은 **진전 쪽으로 기울 때만** 의심한다 — 그게 :data:`ROLE_RESOLUTION`
    이라는 권한의 정의다.
    """
    read, missing = source_summary(sources)

    # 🔑 ``unknown`` 을 **먼저 만들고 성공 경로만 덮어쓴다** (``episode_judge`` 의
    # ``unjudged`` 규율). 새 실패 유형이 생겨도 자동으로 모름으로 떨어진다.
    out: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "verdict": UNKNOWN,
        "reason": None,
        "window": window,
        "min_evidence": min_evidence,
        # 관측기 입력 신고는 **모든** 판정에 실린다. "원장은 못 읽었지만 관측기는
        # 멀쩡했다" 와 "관측기가 눈이 멀었다" 는 처방이 다르기 때문이다.
        "sources_read": read,
        "sources_missing": missing,
        # 신고에서 **아예 빠진** 축(= 축 다는 것을 잊었다). ⛔ "결손"과 다른 사실이다.
        "sources_unreported": None,
        # 🔑 **전 축의 원값**. ``sources_missing`` 은 차단 가능한 권한만 싣고
        # instrument 축을 뺀다 — 그래서 감사용 전수는 여기 따로 남긴다.
        # ⛔ ``dict(sources)`` 를 그대로 싣지 않는다: 축 이름이 문자열이 아닐 수도
        # 있고, 원장은 JSONL 이라 그러면 행이 통째로 안 써진다.
        "sources_report": ({str(k): (v if isinstance(v, bool) else None)
                            for k, v in sources.items()}
                           if isinstance(sources, Mapping) else None),
        # ⛔ 아래 넷은 ``unknown`` 일 때 **계산하지 않은 채로 둔다.** "해결 0건"(사실)과
        # "셀 수 없었다"(모름)가 원장에서 같은 ``0``/``[]`` 로 보이면 이 모듈은 거짓
        # 안심을 파는 장치가 된다. ⛔ ``or 0`` 금지.
        "bought_n": None,
        "window_ids": None,
        "unresolved_ids": None,
        "resolved_ids": None,
        "axes": _axes_block(None, gel_axes),
    }

    # ── 0. 산 것을 못 읽었다. ⛔ ``None``(못 읽었다)을 ``[]``(안 샀다)로 접으면
    # 원장이 깨진 젤이 **콜드스타트로 위장**해 급식이 계속된다 — 정확히 거꾸로다.
    if bought_ids is None:
        return {**out, "reason": REASON_BOUGHT_UNREADABLE}

    # ── 1. 후보 산출 자체가 실패했다. 비교할 한쪽이 없으므로 판정이 성립 안 한다.
    if open_candidates is None:
        return {**out, "reason": REASON_BUILD_FAILED}

    # ── 2. 원소 모양이 깨졌다. 목록은 왔는데 **비교할 수 있는 모양이 아니다** ⇒
    # 그 원소는 열린 id 집합에 못 들어가고, 그만큼 산 후보가 "해결"로 보인다.
    if not all(candidate_shape_ok(c) for c in open_candidates):
        return {**out, "reason": REASON_SCHEMA_INVALID}

    # ── 3. 🔑 스캐너가 **소리 내며** 죽었다.
    # ``work_candidates`` 는 고장 시 결과를 ``tool-error-*`` 후보 하나로 **대체**하므로,
    # 이 줄이 빠지면 산 후보가 전부 사라져 고장이 곧 ``progressing`` 이 된다.
    if any(c.get("source") == META_SOURCE for c in open_candidates):
        return {**out, "reason": REASON_TOOL_ERROR}

    # ── 4. 🔑 스캐너가 **소리 없이** 눈이 멀었다 — 1라운드가 놓친 fail-open.
    # 입력 파일 하나가 사라져도 관측기는 예외도 meta 도 안 내고 그냥 후보를 덜 낸다.
    # ⛔ 신고 부재를 건강으로 접지 않는다. 여기서 접으면 이 검사 전체가 장식이 된다.
    if read is None:
        return {**out, "reason": REASON_SOURCES_UNREPORTED}
    # ── 4b. 🔑 신고가 **전수 목록을 못 덮었다.** 축을 하나 빠뜨린 채로 판정이 나오면
    # 그 입력에 대해서는 2라운드의 구멍이 그대로 재현된다 ⇒ 판정을 거부한다.
    # ⛔ 이 줄이 이 판의 뼈대다. 여기를 지우면 :data:`OBSERVER_INPUTS` 가 장식이 된다.
    gaps = unreported_axes(sources)
    if gaps:
        return {**out, "reason": REASON_SOURCES_INCOMPLETE,
                "sources_unreported": gaps}
    # ── 4c. verdict 권한 축의 결손 ⇒ 판정의 두 팔 중 하나가 못 믿을 값이다.
    # ⛔ ``missing`` 전체가 아니라 **권한으로 거른다** — resolution 권한 축은 여기서
    # 막지 않고 규칙 6b 에서만 막는다(그 축은 진전 쪽으로 기울 때만 의심한다).
    if role_gaps(sources, ROLE_VERDICT):
        return {**out, "reason": REASON_SOURCES_DEGRADED}

    try:
        measure = resolve_payload(PayloadPort(
            7, "agi_v8_1.runtime.progress_stall_payload", "measure_window"))
    except PayloadUnavailable as exc:
        record_critical_failure(exc, site="runtime.progress_stall.payload_unavailable",
                                category="verify")
        # Optional reasoning cannot turn unavailable evidence into permission
        # to feed work. The existing UNKNOWN contract is always blocking.
        return {**out, "reason": "payload_unavailable", "tier": 7,
                "doc_pointer": "Plz_ReadMe.md §T7"}
    counted = measure(bought_ids, open_candidates, window=window,
                      dedup=dedup_keep_last)
    resolved = counted["resolved_ids"]

    # ── 5. 콜드스타트. 여기서 ``stalled`` 로 접으면 새 젤이 **영원히 잠긴다.**
    # 세는 것은 성공했으므로 증거 칸은 채운다 — 이건 모름이 아니라 "아직 부족"이다.
    # (규칙 2를 통과했으므로 모든 원소가 ``id`` 를 가진 Mapping 임이 보장된다.)
    if counted["bought_n"] < min_evidence:
        return {**out, "verdict": INSUFFICIENT, "reason": REASON_INSUFFICIENT,
                **counted, "axes": _axes_block(None, gel_axes)}

    # ── 6. 창 안에서 한 건도 안 사라졌으면 정체다.
    if not resolved:
        return {**out, "verdict": STALLED, "reason": REASON_WINDOW_ZERO,
                **counted, "axes": _axes_block(True, gel_axes)}

    # ── 6b. 🔑 사라지긴 했는데 **이번 스캔이 트리를 덜 봤다.** "고쳐졌다"와
    # "덜 봤다"는 산출물이 똑같다(후보가 목록에서 빠진다) ⇒ 구분 불가 ⇒ 모름.
    # ⛔ 여기서 진전으로 접으면 3라운드가 실측한 뒤집힘(디렉터리 하나 손실 →
    # stalled→progressing, 그때의 축이 전부 True, 원장 흔적 0)이 그대로 살아난다.
    if role_gaps(sources, ROLE_RESOLUTION):
        return {**out, "reason": REASON_RESOLUTION_UNVERIFIABLE,
                **counted, "axes": _axes_block(None, gel_axes)}

    # ── 7. 창 안에서 한 건이라도 사라졌고 규모도 안 줄었다 ⇒ 진전이다.
    return {**out, "verdict": PROGRESSING, "reason": REASON_WINDOW_SOME,
            **counted, "axes": _axes_block(False, gel_axes)}
