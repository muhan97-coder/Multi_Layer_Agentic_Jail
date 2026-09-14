"""# __SLOT_SI_PROPOSED_COMMANDS_CHANNEL_2026_08_02__ proposer → command-exec channel.

The Stage-3/4 command executor (:mod:`agi_v8_1.enforcement.command_executor`) has
been real, armed and wired into the apply ladder since 2026-06-18: the ladder
calls ``_extract_commands`` on every cycle and plans/executes whatever it finds
under ``proposed_commands``. But NO producer ever emitted that key — a grep
showed two READERS and zero writers — so the executor was structurally
unreachable. The 2026-07/08 campaign's ``executor_log`` measured it exactly:
``si_llm_propose`` 282, ``safe_auto_apply`` 289, ``command`` **0**.

This module is the missing producer side, factored ONCE so inc2
(:mod:`llm_failure_proposer`), inc3 (:mod:`objective_proposer`) and inc4
(:mod:`objective_editor`) share a single contract string, a single schema
fragment and a single normaliser — rather than three drifting copies (the
duplicate-prompt trap: the copy you edit is not always the copy that ships).

What this module does NOT do: decide whether a command runs. The proposer only
PROPOSES. ``command_executor.plan_command`` re-validates every string from
scratch and the two operator gates (``AGI_V8_SI_COMMAND_EXEC_ENABLED`` +
``AGI_V8_SI_COMMAND_EXEC_ARMED``) decide execution. With those off, a proposed
command is validated + logged and never run. Nothing here can widen that.

Safety posture:
  - Own default-OFF gate ``AGI_V8_SI_PROPOSED_COMMANDS_ENABLED``. OFF (the
    default) ⇒ :func:`prompt_with_command_channel` returns its argument
    unchanged, :func:`with_command_channel` returns ``dict(schema)`` and
    :func:`parse_commands` returns ``[]`` — i.e. exactly what the lanes built
    before this module existed, byte-identical prompt + byte-identical wire
    schema. (Verified by probe, 2026-08-02: ``prompt_with_command_channel(sp)
    is sp``; ``with_command_channel(s) == dict(s)`` with the original
    ``properties`` object untouched.)

    SCOPE, stated precisely because "OFF ⇒ byte-identical" was claimed too
    broadly — adversarial review (CC-5). The guarantee covers those THREE entry
    points, not every function in the file:

    * :func:`attach_commands` is ungated. It is only ever fed the output of the
      gated :func:`parse_commands` (``[]`` while OFF), so it cannot open the
      channel; but called directly with a non-empty list it WILL add the key
      regardless of the gate. Do not wire it to an ungated producer.
    * ``self_improvement_v8._extract_commands`` — the POD-BLOCK reader, a path
      this gate never covered — now delegates here, and that is a deliberate
      TIGHTENING that applies with the gate off: measured, 12 pod commands →
      8 (``MAX_COMMANDS``), and ``["ls -la", None, 123, "ls -la", "a\\nb"]`` →
      ``["ls -la"]`` where the old inline code produced five entries including
      the FABRICATED commands ``"None"`` and ``"123"`` (it did ``str(item)``).
      Strictly safer, never wider — but not literally byte-identical, so it is
      named here rather than left to be discovered.
  - :func:`attach_commands` never inserts an empty key: no commands ⇒ the result
    dict has no ``proposed_commands`` key at all, so every downstream
    ``.get("proposed_commands")`` sees ``None`` exactly as before.
  - :func:`normalize_commands` is fail-closed on shape: a bare string becomes a
    one-element list, a non-list/non-string (None, int, mapping) becomes ``[]``,
    non-string ITEMS inside a list are dropped rather than stringified, and
    multi-line / oversized / duplicate entries are dropped. Bounded at
    ``MAX_COMMANDS`` PER CALL, and re-applied by the cycle over the cross-lane
    concatenation. Every drop is counted by reason and logged (F4).
  - Commands are proposed by the same untrusted responses that propose patches,
    so they are subject to the SAME quality decisions: under best-of-N only the
    WINNING candidate's commands survive, a response judged malformed drops its
    commands with it (F3), and when the inc5 verify gate is armed only a GREEN
    verdict lets commands through (F2).

Adversarial review 2026-08-02 (all findings verified against running code before
fixing): the contract text below once advertised ``awk``/``sqlite3``/``pandoc``/
``pylint`` as safe read-only heads. They are interpreters — ``awk
'BEGIN{system(…)}'``, ``sqlite3 -cmd ".shell …"`` and ``pandoc --lua-filter=…``
each wrote a file OUTSIDE ``state_dir`` while ``plan_command`` reported
``allowed=True category="read_only"``, because path containment only inspects
PATH-SHAPED operands and a code payload is not one. Fixed in
``enforcement/command_executor.py`` (``_CODE_OPERAND_HEADS`` +
``_HEAD_DENIED_OPTIONS``); the contract now describes the policy that actually
runs. Do not re-add a head to the advertised list without re-running the escape
probe against ``plan_command`` + ``execute_planned``.

Adversarial review round 2, 2026-08-02 — three more contract/policy defects,
each reproduced against running code before it was touched:

CC-1 (contract omitted two whole classes of guaranteed refusal). The contract
invited exactly the uses that cannot succeed. (a) ``check_deny`` runs on the
JOINED string, so a deny pattern anywhere in an ARGUMENT kills the command
regardless of head: ``grep -rn sudo .``, ``grep -rn 'rm -rf' .``,
``rg -n 'git push --force' .``, ``git log --grep=sudo`` all measured DENY.
(b) ``_path_operand_escape`` appends EVERY non-dash token as a path candidate,
search patterns included: ``grep -n '/etc/passwd' si_lanes/a.py`` → DENY
path_escape, on the pattern. The old text said containment "is checked on
PATH-SHAPED operands", which told the model the opposite. This bit because the
contract's own worked examples ("counting occurrences across the workspace")
are aimed at a codebase whose SUBJECT MATTER is destructive-command policy —
and because a refusal is terminal, each one burned a slot out of MAX_COMMANDS
invisibly (they pass ``normalize_commands`` and die later, at plan time, so the
drop census never sees them). Both rules are now stated, with the workaround.

CC-2 (the "a command that writes is refused" claim was FALSE). Measured with
both exec gates armed: ``sort -o F in.txt``, ``sort -oF in.txt``,
``sort --output F``, ``sort --output=F``, ``git diff|log|show|blame --output=F``
and ``git diff --output F`` — NINE spellings — each planned ``allowed=True
category=read_only`` and CREATED THE FILE. ``sort -o mc_runs/place_log.jsonl
junk.txt`` overwrote a pre-image ledger row. Same invariant that got
``black``/``isort`` denied, same class, missed. Fixed in
``_HEAD_DENIED_OPTIONS``; the contract now states the rule as "read-only
INVOCATIONS, not read-only binaries" rather than a list to probe around.

CC-3 (the contract published the escape menu). Enumerating "awk, sqlite3,
pandoc, pylint, black, isort … find -exec, sort --compress-program, rg --pre,
git -c" to an untrusted model is a denylist disclosure whose neighbourhood was
unguarded — a model told ``sort --compress-program`` is refused probes siblings
and lands on ``sort -o`` (CC-2). The enumeration is gone; the contract now
states the positive rule only. Keep it that way.

CONTRACT v2, 2026-08-02 (__SLOT_SI_CMD_CONTRACT_V2_2026_08_02__) — the wiring
worked and the channel still emitted nothing. Measured live, best-of-N=8,
deepseek-v4-flash + thinking=high, isolated jail: prompt_injected 8/8,
schema_injected 8/8, ``proposed_commands`` emitted **0/8** — and all eight
candidates then FABRICATED the number the objective asked for ("Total files: 1"
against a workspace holding nine). So the failure was never the wire; it was
that the contract argued the model out of using the field:

  - 87.8% of the text was prohibition, 12.2% was purpose, and all SEVEN worked
    examples were REFUSALS. The only concrete picture of the field a model had
    was "this is how you get rejected".
  - it stated an explicit dominant strategy: omitting is "never penalised"
    (cost 0) while proposing risks a "WASTED slot", "never run, never retried,
    never repaired". Nothing on the other side of that ledger.
  - it never said what the workspace CONTAINS, so the model could not judge
    whether a command would even answer its question. The jail is the run's
    state_dir (ledgers + earlier proposals), NOT the source tree the payload
    quotes — a distinction the old text left the model to discover by refusal.

RESULT, measured the same way (isolated jail, N=8, same objective, exec gate
ARMED deliberately unset so commands are planned and never run): **0/8 → 8/8**,
and every command emitted (``ls -la``, ``find . -type f``, ``find . -type d``,
``find . -type f -printf '%f\\n'``, …) passed ``plan_command`` — zero refusals.
An A/B/C third arm ran contract v2 with the ``providers/base.py`` optional-keys
clause OFF and also measured **8/8**, so the emission is attributable to the
CONTRACT TEXT, not to the placement lever. Cost: $0.015 (before) + $0.036
(after) + $0.035 (attribution arm).

v2 keeps every refusal rule the adversarial rounds pinned (they were accurate;
the tests above still assert each one) and changes what surrounds them: a
when-to-use test ("would you otherwise be GUESSING?"), an inventory of what the
sandbox actually holds, THREE examples that PASS (``ls -la``,
``wc -l cycle_log.jsonl``, ``grep -rln VALUE si_proposed`` — each verified
against ``plan_command`` in a seeded jail), and the fact that every verdict —
refusals included, with category and reason — lands in this same workspace's
``apply_chain.jsonl`` under ``decision_source="command_exec"``, so a refusal is
observable and a later cycle can read it back. Composition after: purpose +
examples + feedback 54%, rules 46%.

Two placement facts drove where the words went (measured, ``probe_recorder``):
the lanes pass their prompt as the ``prompt`` ARGUMENT, never as
``payload["system_prompt"]``, so ``COMMAND_CONTRACT`` lands in the WIRE USER
message while the WIRE SYSTEM message ends on "MUST include ALL of these
top-level keys: \"files\"". The SCHEMA, however, IS serialised into the system
role — hence the enriched ``COMMAND_SCHEMA_PROPERTY["description"]`` above (the
channel's only high-authority foothold), and the schema-driven optional-keys
clause in ``providers/base.py`` (default-OFF gate
``AGI_V8_SCHEMA_OPTIONAL_KEYS_HINT_ENABLED``) that stops that trailing line
from reading as an exclusive key list. ``required`` is still NEVER touched —
forcing the key would buy emission by making the model invent commands.

Honest scope of that last lever: the A/B/C above shows it was NOT needed for
emission (contract v2 alone measured 8/8). It stays because the sentence it
fixes is independently wrong — ``required`` is a MINIMUM and the English said
"ALL of these top-level keys" — but it must not be credited with the result.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Mapping

# __SLOT_FAIL_FAST_2026_07_25__ Swallowed failures route through one choke
# point: counted + named always, re-raised under AGI_V8_STRICT_FAIL_FAST.
from agi_v8_1.policy.fail_fast import swallowed as _swallowed

logger = logging.getLogger(__name__)

_ENABLE_ENV = "AGI_V8_SI_PROPOSED_COMMANDS_ENABLED"

# Bounds (defense in depth). NOTE (adversarial-review-3, F6): this bound is
# PER CALL. inc2/inc3/inc4 each append to one per-cycle list, so the cycle-level
# cap is re-applied by ``self_improvement_v8`` running this same normaliser over
# the CONCATENATION (__SLOT_SI_CMD_GLOBAL_CAP_2026_08_02__) — without that the
# seam could receive 3×8 commands with no cross-lane dedup.
MAX_COMMANDS = 8
MAX_COMMAND_CHARS = 400

# Characters that make a "single command string" not a single command string.
_FORBIDDEN_CHARS = ("\n", "\r", "\x00")

# The JSON-schema fragment advertised to the model. OPTIONAL by construction:
# it is added to ``properties`` and NEVER to ``required``, so a model that emits
# no commands still satisfies the schema (and the required-keys reinforcement
# the provider appends stays unchanged).
#
# __SLOT_SI_CMD_CONTRACT_V2_2026_08_02__ the DESCRIPTION is not decoration: the
# provider serialises the whole schema into the WIRE SYSTEM message
# (``_build_system_prompt`` → "Target JSON schema:\n{...}"), while the contract
# prose below rides in the USER message. Measured 2026-08-02: contract in
# SYSTEM=False, in USER=True, starting 15% into a 4523-char user blob. So this
# string is the ONLY part of the channel that reaches the high-authority role —
# it carries the invitation and two worked examples, not just the prohibitions.
COMMAND_SCHEMA_PROPERTY: dict[str, Any] = {
    "type": "array",
    "items": {"type": "string"},
    "description": (
        "OPTIONAL observation channel. Include it when your answer would "
        "otherwise contain a GUESSED fact about the sandboxed workspace — a "
        "count, an inventory, whether a file is already there — and omit the "
        "key entirely otherwise. Omitting is the normal case and is never "
        "penalised. Read-only argv strings, one command per string, run with "
        "this run's state directory as the working directory; the outcome of "
        "each (allowed, or refused with a reason) is recorded in that same "
        "directory's apply_chain.jsonl. Examples that pass the executor "
        "policy: \"ls -la\", \"wc -l cycle_log.jsonl\", "
        "\"grep -rln VALUE si_proposed\". No shell operators, read-only heads "
        "only, relative paths that stay inside the workspace. The full policy "
        "is in the <proposed_commands> section of the instructions."
    ),
}

# The contract text appended to a lane's system prompt. Every claim here was
# read off ``enforcement/command_executor.py`` + ``executor_command_policy.py``
# and verified empirically against ``plan_command`` — it describes the policy
# that actually runs, not an aspiration.
COMMAND_CONTRACT: str = (
    "<proposed_commands optional=\"true\">\n"
    "WHAT IT IS. An OPTIONAL top-level \"proposed_commands\" array of argv "
    "strings, run read-only inside the sandboxed workspace before your output "
    "is reviewed. It exists for the one thing writing files cannot do for you: "
    "LOOK at something before you commit to an answer.\n"
    "\n"
    "WHEN TO USE IT — one test. Does your work product state a fact about the "
    "workspace that you would otherwise be GUESSING? A count, an inventory, "
    "whether a file is already there, what an earlier proposal put in it. If "
    "yes, ask for it: a guessed number is a wrong answer that reads like a "
    "right one. If your output follows from the payload you were handed, omit "
    "the key. Omitting is the normal case, it is never penalised, and an "
    "unused channel costs you nothing.\n"
    "\n"
    "WHAT IS IN THE WORKSPACE. The working directory is THIS RUN'S state "
    "directory — what the loop itself has written, not the project source "
    "tree. Typically: earlier proposals under si_proposed/, and the run's own "
    "ledgers (cycle_log.jsonl, apply_chain.jsonl, runtime_logs/*.jsonl). The "
    "files quoted to you in the payload live OUTSIDE it and no command can "
    "reach them — you already have their full contents, so do not ask.\n"
    "\n"
    "EXAMPLES THAT RUN, verified against the policy that validates these:\n"
    "  \"ls -la\"                        what the workspace actually holds\n"
    "  \"wc -l cycle_log.jsonl\"         how many rows a ledger really has\n"
    "  \"grep -rln VALUE si_proposed\"   which earlier proposals mention a token\n"
    "Ask one concrete question per command, in as few commands as possible, "
    "and read the answer off stdout.\n"
    "\n"
    "WHAT COMES BACK. Every string is re-validated by an independent executor "
    "policy, and proposing a command is NOT permission to run one — an "
    "operator gate makes that call, and while it is off each command is "
    "validated and logged but not run. Either way the verdict for every "
    "command (allowed, or refused with its category and reason) is appended to "
    "this run's apply_chain.jsonl under decision_source=\"command_exec\", "
    "inside this same workspace. A refusal is therefore recorded rather than "
    "silent, and a later cycle can read it back with "
    "`grep -n command_exec apply_chain.jsonl` and adjust. Nothing you propose "
    "here can change the workspace.\n"
    "\n"
    "THE RULES, short (the policy refuses; it never repairs):\n"
    "- argv only, one command per string. ANY shell operator "
    "(| & && || ; > >> < << 2>) is refused. There is no shell: no pipes, no "
    "redirection, no command substitution, no `bash -c`.\n"
    "- Allowlisted read-only heads only: ls, cat, head, tail, wc, sort, grep, "
    "rg, find, diff, stat, du, df, date, pwd, echo, printf, jq, sha256sum, "
    "md5sum, realpath, basename, dirname, which, and the object-only git "
    "subcommands (git log / show / rev-parse). Git worktree consumers are "
    "refused because repository-local config can execute helpers. Any other "
    "head is refused, including every interpreter, every "
    "network tool, and every head whose OPERAND is a program rather than data. "
    "You cannot run code or reach the network from this field.\n"
    "- READ-ONLY INVOCATIONS only, not merely read-only binaries: any option "
    "that makes an allowlisted head write a file, or hand work to another "
    "program, is refused too. Print to stdout and read the output.\n"
    "- Relative paths only, inside the workspace. Path containment applies to "
    "EVERY non-flag operand and the checker cannot tell a path from a pattern: "
    "any token not starting with `-` is a path candidate, and if it is "
    "absolute, contains \"..\", or resolves outside the sandboxed workspace "
    "the command is refused. `grep -n '/etc/passwd' a.py` is refused for the "
    "PATTERN, not the file — so never write a path-shaped pattern.\n"
    "- THE WHOLE STRING IS SCANNED FOR DESTRUCTIVE COMMANDS, ARGUMENTS "
    "INCLUDED, so a search term that itself reads as a dangerous command "
    "refuses its own command: `grep -rn sudo .` is refused even though grep "
    "only reads. Search for a narrower, non-command token (an identifier, a "
    "message fragment).\n"
    f"- At most {MAX_COMMANDS} commands, each at most {MAX_COMMAND_CHARS} "
    "characters, each a single line. Commands run with the workspace as the "
    "working directory and a stripped environment (no credentials reach "
    "them).\n"
    "</proposed_commands>"
)


def enabled() -> bool:
    """Default-OFF gate for the proposer-side command channel.

    Strict ``"true"``/``"1"`` (the convention the sibling F1 lane gates use:
    ``_si_code_proposer_enabled`` / ``_si_llm_proposer_enabled`` /
    ``_si_verify_gate_enabled`` all parse this way).

    __SLOT_SI_CMD_GATE_TRAP_2026_08_02__ adversarial-review-3 fix (F7): the
    EXECUTOR's sibling gates (``command_exec_enabled``) parse
    ``.strip().lower()``, so ``…=True`` turns the executor on but left this gate
    silently off — fail-closed, but a silent operator trap. The strict parse
    stays (sibling consistency wins per the project gate convention); the trap
    does not: an unrecognised non-empty value now WARNS instead of vanishing.
    """
    raw = os.environ.get(_ENABLE_ENV, "")
    if raw in ("true", "1"):
        return True
    if raw.strip() and raw.strip().lower() in ("true", "1", "yes", "on"):
        logger.warning(
            "%s=%r is NOT recognised (strict %r/%r only) — the proposer command "
            "channel stays OFF. Note the executor gates (AGI_V8_SI_COMMAND_EXEC_"
            "ENABLED/_ARMED) parse case-insensitively, so they may be ON.",
            _ENABLE_ENV, raw, "true", "1",
        )
    return False


def prompt_with_command_channel(system_prompt: str) -> str:
    """Append the command contract to *system_prompt* (gate ON), else identity.

    Gate OFF returns the SAME object, so a caller cannot accidentally ship a
    re-joined/normalised variant of the historical prompt bytes.
    """
    if not enabled():
        return system_prompt
    return f"{system_prompt}\n{COMMAND_CONTRACT}"


def with_command_channel(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Return *schema* + the OPTIONAL ``proposed_commands`` property (gate ON).

    Gate OFF returns ``dict(schema)`` — exactly what the lanes passed before.
    Never touches ``required``: the field stays optional, so a model that emits
    no commands still satisfies the schema.
    """
    out = dict(schema)
    if not enabled():
        return out
    props = dict(out.get("properties") or {})
    props["proposed_commands"] = dict(COMMAND_SCHEMA_PROPERTY)
    out["properties"] = props
    return out


def normalize_commands(raw: Any) -> list[str]:
    """Coerce a model-supplied ``proposed_commands`` value into a safe list.

    Fail-closed on shape (the model is untrusted input, not an API):
      - ``str``            → one-element list (a model that emits a bare string
                             instead of an array is honoured, not silently lost)
      - ``list``/``tuple`` → non-``str`` items are DROPPED (never ``str()``-ed:
                             stringifying ``None``/``123``/a dict would fabricate
                             a command out of a malformed field)
      - anything else      → ``[]`` (None, int, Mapping, …)
    Each surviving item is stripped, and dropped when empty, longer than
    ``MAX_COMMAND_CHARS``, containing a newline/NUL, or already seen. Order is
    preserved; the result is capped at ``MAX_COMMANDS``.

    Returns ``[]`` (never raises) for every malformed input, so a lane can call
    this unconditionally on whatever JSON came back.

    __SLOT_SI_CMD_DROP_OBSERVABILITY_2026_08_02__ adversarial-review-3 fix (F4):
    every drop is COUNTED BY REASON and logged. Before, five ``continue``/
    ``break`` paths dropped silently and the lanes logged only the POST-drop
    count — so a model returning ``"grep -rn X .\\nwc -l Y"`` (one string, two
    lines: a very common shape) produced ``commands=0``, byte-identical to the
    "no producer exists" failure this whole channel was built to fix. Same wrong
    diagnosis, reproduced. Drop rate must be observable (feedback_observability_first).
    """
    if isinstance(raw, str):
        items: list[Any] = [raw]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        if raw is not None:
            logger.info(
                "command channel: proposed_commands has unusable type %s — "
                "dropped whole (expected list[str] or str)", type(raw).__name__,
            )
        return []

    out: list[str] = []
    seen: set[str] = set()
    drops: dict[str, int] = {}

    def _drop(reason: str) -> None:
        drops[reason] = drops.get(reason, 0) + 1

    for item in items:
        if not isinstance(item, str):
            _drop("non_string_item")
            continue
        s = item.strip()
        if not s:
            _drop("empty")
            continue
        if len(s) > MAX_COMMAND_CHARS:
            _drop("over_max_chars")
            continue
        if any(c in s for c in _FORBIDDEN_CHARS):
            # The multi-line case: the model put several commands in ONE string.
            _drop("multiline_or_nul")
            continue
        if s in seen:
            _drop("duplicate")
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= MAX_COMMANDS:
            if len(items) > len(out):
                _drop("over_max_commands")
            break
    if drops:
        logger.info(
            "command channel: kept %d/%d proposed command(s); dropped=%s",
            len(out), len(items),
            ", ".join(f"{k}={v}" for k, v in sorted(drops.items())),
        )
    return out


def parse_commands(raw_json: Any) -> list[str]:
    """Extract + normalise ``proposed_commands`` from a raw propose_fn response.

    Fail-safe: non-string input, malformed JSON, non-object JSON or a missing
    key all degrade to ``[]`` — a lane calls this next to its own parser and one
    bad best-of-N candidate must not abort the others. Returns ``[]``
    unconditionally while the gate is OFF, so a model volunteering the key
    cannot open the channel on its own (and the gate-OFF path parses nothing, so
    it cannot count a swallow the pre-channel code did not count either).

    Call this AFTER the lane's own parser: both see the same malformed string,
    and under ``AGI_V8_STRICT_FAIL_FAST`` the ORIGINAL parser's site should be
    the one that raises, not this newer one.
    """
    import json

    if not enabled():
        return []
    if not isinstance(raw_json, str):
        return []
    try:
        obj = json.loads(raw_json)
    except Exception as _ff_exc:  # noqa: BLE001 — fail-safe parser: never raises.
        # __SLOT_SI_CMD_SWALLOW_DEDUP_2026_08_02__ adversarial-review-3 fix (F7).
        # A plain decode failure is NOT ours to count: every caller runs its OWN
        # parser over these same bytes first (the documented ordering above) and
        # already counted the identical failure at its own site. Counting it
        # again added a SECOND ``swallowed[verify]`` entry per malformed
        # response, shifting the ``census()`` baseline the fail-fast audit reads.
        # A non-decode failure is genuinely unexpected and is NOT what the lane
        # parsers catch (``_parse_proposal`` catches only ValueError/TypeError),
        # so that one stays counted here.
        # __SLOT_CENSUS_UNCONDITIONAL_RECORD_2026_08_03__ 중복 방지는 유지하되
        # **조건부 기록**을 그만둔다. 조건이 거짓인 경로에서 흔적이 0 이면
        # fail-closed census 가 이 핸들러를 침묵으로 세기 때문이다. 대신 중복분을
        # **다른 site 키**로 보내 원래 site 의 기준선은 그대로 두고 중복이 숨지
        # 않고 보이게 한다(0 을 "안 삼켰다"로 오독하지 않는다).
        #
        # strict fail-fast 동작은 실질적으로 안 바뀐다: 위 docstring 의 순서대로
        # 레인 자신의 파서가 **먼저** 같은 바이트를 파싱하고, JSONDecodeError 는
        # ValueError 의 하위형이라 그 파서가 잡아 초크포인트에 넘긴다 — strict 면
        # 거기서 이미 raise 되므로 이 줄에 도달하지 않는다.
        _swallowed(
            _ff_exc,
            site=("si_lanes.command_channel.parse_commands:decode_dup"
                  if isinstance(_ff_exc, json.JSONDecodeError)
                  else "si_lanes.command_channel.parse_commands"),
            category="verify",
        )
        return []
    if not isinstance(obj, Mapping):
        return []
    return normalize_commands(obj.get("proposed_commands"))


def attach_commands(result: dict[str, Any], commands: Any) -> dict[str, Any]:
    """Put ``proposed_commands`` on *result* ONLY when there is something to put.

    Empty/malformed ⇒ the key is NOT created, so the result dict is byte-
    identical to the pre-channel shape and every downstream ``.get()`` behaves
    exactly as before. Mutates and returns *result* for call-site brevity.
    """
    cmds = normalize_commands(commands)
    if cmds:
        result["proposed_commands"] = cmds
    return result


__all__ = [
    "COMMAND_CONTRACT",
    "COMMAND_SCHEMA_PROPERTY",
    "MAX_COMMANDS",
    "MAX_COMMAND_CHARS",
    "attach_commands",
    "enabled",
    "normalize_commands",
    "parse_commands",
    "prompt_with_command_channel",
    "with_command_channel",
]
