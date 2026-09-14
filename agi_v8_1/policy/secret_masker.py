# R25 W4 (ported from v7.1 v7/policy/secret_masker.py + v7/causal_trace.py)
"""Self-contained secret masker for V8 (R9.S7 + R9.1 invariants preserved).

V8 port inlines the canonical regex patterns from v7.1's causal_trace so
this module has zero v7.1 dependency. R9.S7 bounded-recursion + cycle guard
and R9.1 key-also-mask rules are preserved byte-for-byte.

Pattern set (order matters: multi-line private-key blocks stripped first
so a key body cannot leak through a narrower token match):

  1. PEM private key blocks (RSA/EC/DSA/OPENSSH/PGP)
  2. JWT (three base64 segments)
  3. OpenAI / Anthropic sk-... tokens
  4. AWS access key ids (AKIA...)
  5. Google API keys (AIza...)
  6. GitHub tokens (ghp_/gho_/ghu_/ghs_/ghr_/gha_)
  7. Slack tokens (xoxb-/xoxa-/xoxp-/xoxr-/xoxs-)
  8. Basic / Bearer auth headers
  9. Quoted JSON/Python repr structured secrets
 10. Generic kv-form (api_key=..., password=..., token=..., etc.)
"""

from __future__ import annotations

import ast
import json
import re
import textwrap
from typing import Any

# __SLOT_FAIL_FAST_2026_07_25__ Swallowed failures route through one choke
# point: counted + named always, re-raised under AGI_V8_STRICT_FAIL_FAST.
from agi_v8_1.policy.fail_fast import swallowed as _swallowed

_MASK = "***MASKED***"
_PK_MASK = "***PRIVATE_KEY_MASKED***"

_SECRET_KEY_NAME_SRC = (
    r"(?:api[_-]?key|secret(?:[_-]?key)?|client[_-]?secret|"
    r"access[_-]?key|password|passwd|pwd|token|auth(?:[_-]?token)?|"
    r"authorization|private[_-]?key|credential(?:s)?|bearer)"
)

_SECRET_KEY_EXACT = {
    "api_key",
    "apikey",
    "secret",
    "secret_key",
    "secretkey",
    "client_secret",
    "clientsecret",
    "access_key",
    "accesskey",
    "password",
    "passwd",
    "pwd",
    "token",
    "auth",
    "auth_token",
    "authtoken",
    "authorization",
    "private_key",
    "privatekey",
    "credential",
    "credentials",
    "bearer",
}

# R9.S7 traversal limits
_MAX_RECURSION_DEPTH = 8
_MAX_CONTAINER_ITEMS = 1000

_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # __SLOT_W1A2__: Pattern #1 extended to cover ENCRYPTED PEM (PKCS#8 enc),
    # plain "PRIVATE KEY" (PKCS#8), SSH2 (4-dash), and PuTTY .ppk blocks.
    (
        re.compile(
            r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?"
            r"PRIVATE KEY-----"
            r".*?"
            r"-----END (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?"
            r"PRIVATE KEY-----",
            re.DOTALL,
        ),
        _PK_MASK,
    ),
    # __SLOT_W1A2__: SSH2 (RFC 4716) 4-dash header private key blocks.
    (
        re.compile(
            r"---- BEGIN SSH2 (?:ENCRYPTED )?PRIVATE KEY ----"
            r".*?"
            r"---- END SSH2 (?:ENCRYPTED )?PRIVATE KEY ----",
            re.DOTALL,
        ),
        _PK_MASK,
    ),
    # __SLOT_W1A2__: PuTTY .ppk private key file content. The .ppk format
    # begins with `PuTTY-User-Key-File-<version>:` header followed by
    # key body lines including `Private-Lines:` and base64. Mask from the
    # PuTTY header through the trailing Private-MAC line (final line of file).
    (
        re.compile(
            r"PuTTY-User-Key-File-\d+:.*?Private-MAC:\s*[0-9a-fA-F]+",
            re.DOTALL,
        ),
        _PK_MASK,
    ),
    # __SLOT_BOUNDARY_FIX_2026_06_16__ — the leading token anchor is
    # ``(?<![A-Za-z])`` (NOT ``\b``). A ``\b`` only fires at a word/non-word
    # transition, so a secret glued directly to a word char (``lane_sk-...``,
    # ``token_sk-...``, ``1sk-...``, an f-string ``f'{m}_{key}'``) had NO
    # boundary before it and the pattern silently missed it — the raw key
    # leaked into every masked trail. ``(?<![A-Za-z])`` instead fires after
    # ``_`` / digit / ``=`` / ``:`` / quote / whitespace / string-start (the
    # realistic glue) while still NOT firing mid-English-word (``risk-``,
    # ``task-``, ``ask-``, ``disk-``), so domain words like ``task-scheduler``
    # are preserved. Residual: a secret glued immediately after a LETTER with
    # no separator (``KEYsk-...``) — pathological, and key:value forms are
    # caught by the key-name patterns below regardless.
    (
        re.compile(
            r"(?<![A-Za-z])eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
        ),
        _MASK,
    ),
    (re.compile(r"(?<![A-Za-z])sk-(?:ant-)?[A-Za-z0-9_-]{16,}"), _MASK),
    (re.compile(r"(?<![A-Za-z])AKIA[0-9A-Z]{16}\b"), _MASK),
    (re.compile(r"(?<![A-Za-z])AIza[0-9A-Za-z_-]{30,}\b"), _MASK),
    # __SLOT_W1A2__: Pattern #6 typo fix: char-class was `[pousr]`, missing
    # `a` despite docstring listing `gha_` GitHub Actions tokens. Now
    # `[pousra]` so `gha_` token prefixes are masked.
    (re.compile(r"(?<![A-Za-z])gh[pousra]_[A-Za-z0-9]{20,}\b"), _MASK),
    (re.compile(r"(?<![A-Za-z])xox[baprs]-[A-Za-z0-9-]{10,}"), _MASK),
    # Basic credentials include +, / and padding; masking only the scheme
    # would leave the reversible username/password payload in request content.
    (
        re.compile(
            r"(?i)(\b(?:proxy-)?authorization\s*[:=]\s*['\"]?)"
            r"basic[ \t\r\n]+[A-Za-z0-9._~+/\-]+=*"
        ),
        r"\1" + _MASK,
    ),
    (re.compile(r"(?i)(?<![A-Za-z])bearer\s+[A-Za-z0-9._\-]{16,}"), "bearer " + _MASK),
    # __SLOT_MASKER_URI_USERINFO_2026_08_25__ R21 §9 구멍 (a): connection-string
    # userinfo (``scheme://user:pass@host``) — postgres/mysql/redis/mongodb+srv/
    # amqp 등 어떤 프리픽스도 위 특정-공급자 패턴에 안 걸리고, 일반 kv 패턴도
    # 안 물었다(``:``/``@`` 로 감싸인 값이라 key=value 도 quoted-kv 모양도 아니다).
    # scheme 은 RFC 3986 스킴 문자 집합(``[a-zA-Z][a-zA-Z0-9+.-]*``)으로 좁혀
    # 일반 문장의 ``foo: bar@baz`` 류를 안 문다 — ``://`` 리터럴이 반드시 있어야
    # 한다. 자격증명 없는 URL(``https://example.com/x``)은 ``user:pass@`` 모양이
    # 아니라 무변화. 사용자명은 그대로 두고(디버깅에 유용, 사용자명 자체는
    # 거의 비밀이 아니다) 비밀번호 구간만 가린다.
    (
        re.compile(
            r"(?i)(?P<uriprefix>\b[a-z][a-z0-9+.-]{1,15}://[^\s'\"@/:]+:)"
            r"(?P<uripass>[^\s'\"@/]+)(?P<urisuffix>@)"
        ),
        r"\g<uriprefix>" + _MASK + r"\g<urisuffix>",
    ),
    (
        re.compile(
            rf"(?i)(?P<prefix>(?P<keyquote>['\"]){_SECRET_KEY_NAME_SRC}"
            rf"(?P=keyquote)\s*:\s*)(?P<valuequote>['\"])[^'\"\r\n]{{0,512}}"
            rf"(?P=valuequote)"
        ),
        r"\g<prefix>\g<valuequote>" + _MASK + r"\g<valuequote>",
    ),
    # __SLOT_W1A2__: Pattern #9b — JSON-in-string with escaped quotes
    # (literal backslash-quote sequences). Matches form
    # `\"<key>\":\"<value>\"` and replaces value with mask while preserving
    # the literal backslash-quote framing. The original Pattern #9 fails
    # because (?P=keyquote) backreference cannot match the `\` preceding
    # the actual `"` in escaped JSON nested inside another string.
    # Replacement uses a tuple sentinel (None) — handled in mask_text loop.
    (
        re.compile(
            rf"(?i)(?P<prefix>\\\"{_SECRET_KEY_NAME_SRC}\\\")"
            rf"(?P<sep>\s*:\s*)"
            rf"\\\"(?P<value>[^\"\\\r\n]{{0,512}})\\\""
        ),
        # Use a callable to bypass re.sub backslash-escape rules in the
        # replacement string (raw `\"` would otherwise be misinterpreted).
        lambda m: m.group("prefix") + m.group("sep") + '\\"' + _MASK + '\\"',
    ),
    (
        re.compile(
            rf"(?i)(?P<prefix>\b{_SECRET_KEY_NAME_SRC}\b\s*:\s*)"
            rf"(?P<valuequote>['\"])[^'\"\r\n]{{0,512}}(?P=valuequote)"
        ),
        r"\g<prefix>\g<valuequote>" + _MASK + r"\g<valuequote>",
    ),
    (
        re.compile(
            rf"(?i)\b(({_SECRET_KEY_NAME_SRC})\s*[:=]\s*)"
            r"['\"]?[^\s'\",}\]]{1,}['\"]?"
        ),
        r"\1" + _MASK,
    ),
]


def _normalise_key_name(key: Any) -> str:
    text = str(key or "")
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    return re.sub(r"_+", "_", text)


def _is_secret_key(key: Any) -> bool:
    """Return True for structured fields whose values must be redacted."""
    if not isinstance(key, str):
        return False
    norm = _normalise_key_name(key)
    if norm in _SECRET_KEY_EXACT:
        return True
    parts = set(norm.split("_"))
    return (
        {"api", "key"}.issubset(parts)
        or {"secret", "key"}.issubset(parts)
        or {"client", "secret"}.issubset(parts)
        or {"access", "key"}.issubset(parts)
        or {"private", "key"}.issubset(parts)
        or {"auth", "token"}.issubset(parts)
    )


def mask_text(text: Any) -> Any:
    """Mask API keys / passwords / private keys in *text*.

    Non-str values pass through unchanged so this is safe to call on any
    leaf during recursive masking.
    """
    if not isinstance(text, str) or not text:
        return text
    out = text
    for pat, repl in _SECRET_PATTERNS:
        out = pat.sub(repl, out)
    return out


def mask_secrets_strict(text: Any) -> str:
    """Strict COMPOSED string masker for log/trail fields that may echo an
    exception message carrying a key.

    ``mask_text`` uses an ``sk-(?:ant-)?...{16,}`` threshold and so MISSES short
    prefixed keys (``sk-{6,}`` / ``xai-`` / ``AIza`` / ``Bearer ...``) that
    ``providers.base._mask_secret`` catches. Composing both is the canonical
    gate for raw exception strings (executor-log raw_dump/error, swarm
    ``LANE_DONE`` error). Single source of truth — callers must not re-implement
    the composition.

    ``providers.base`` is imported LAZILY so this module stays stdlib-only at
    import time (callers can be lazily imported into AST-restricted modules such
    as ``executor_v20`` without pulling ``providers`` at their import). Always
    returns ``str``. If that import fails outright, the content is WITHHELD
    behind a named sentinel instead of propagating — see
    :func:`_strict_masker_unavailable`.
    """
    masked = mask_text(str(text))
    # Lazy import — keep secret_masker stdlib-only at module-import time.
    try:
        from agi_v8_1.providers.base import _mask_secret
    except Exception as exc:  # noqa: BLE001 — import 실패가 호출자의 돈 행을 먹으면 안 된다
        _swallowed(exc, site="policy.secret_masker.mask_secrets_strict:import",
                   category="telemetry")
        return _strict_masker_unavailable(exc)

    return _mask_secret(masked)


def _request_annotation_edits(source: str) -> list[tuple[int, int, bytes]]:
    """UTF-8 edits for proven declarations, never arbitrary type-like text.

    Parsing is not evidence that a bare ``password: str`` is source: it is also
    YAML. Require actual function/class scope. String annotations, calls and
    literal type parameters are deliberately not exempted. No code is executed.
    """
    tree = ast.parse(source)
    roots = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    if not roots:
        return []
    raw = source.encode("utf-8")
    starts = [0]
    for line in raw.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))

    def offset(node: ast.AST, end: bool = False) -> int:
        return starts[getattr(node, "end_lineno" if end else "lineno") - 1] + getattr(
            node, "end_col_offset" if end else "col_offset")

    edits: dict[tuple[int, int], bytes] = {}
    nodes = [node for root in roots for node in ast.walk(root)]
    if len(nodes) > 16_384:
        return []
    defaults: dict[int, ast.AST] = {}
    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            positional = args.posonlyargs + args.args
            if args.defaults:
                defaults.update((id(a), d) for a, d in zip(positional[-len(args.defaults):], args.defaults))
            defaults.update((id(a), d) for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None)
    allowed = (ast.Name, ast.Attribute, ast.Subscript, ast.Tuple, ast.List,
               ast.BinOp, ast.BitOr, ast.Load)

    def credential_resolution(value: ast.AST, name: str) -> bool:
        # Deliberately narrow constructor plumbing, not arbitrary calls or
        # booleans (which could contain a literal PIN or a secret-like name).
        match value:
            case ast.Call(
                func=ast.Attribute(attr="strip", value=ast.BoolOp(op=ast.Or(), values=[
                    ast.Name(id=key_name),
                    ast.Call(func=ast.Attribute(attr="get", value=ast.Attribute(
                        attr="environ", value=ast.Name(id="os"))),
                        args=[ast.Name(), ast.Constant(value="")], keywords=[]),
                ])), args=[], keywords=[],
            ):
                return key_name == name
        return False

    for node in nodes:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if not isinstance(target, (ast.Name, ast.Attribute)):
                continue
            name = target.id if isinstance(target, ast.Name) else target.attr
            if not _is_secret_key(name) or not credential_resolution(node.value, name):
                continue
            target_end = offset(target, end=True)
            prefix = raw[target_end:offset(node.value)]
            if re.fullmatch(rb"\s*=(?:\s|\()*", prefix) is not None:
                equal = target_end + prefix.index(b"=")
                edits[(equal, equal + 1)] = b"\x01"
            continue
        if isinstance(node, ast.arg):
            name, annotation, value = node.arg, node.annotation, defaults.get(id(node))
            target_end = offset(node) + len(name.encode("utf-8"))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, (ast.Name, ast.Attribute)):
            name = node.target.id if isinstance(node.target, ast.Name) else node.target.attr
            annotation, value = node.annotation, node.value
            target_end = offset(node.target, end=True)
        else:
            continue
        if annotation is None or not _is_secret_key(name):
            continue
        if any(not isinstance(n, allowed) and not (isinstance(n, ast.Constant) and n.value is None)
               for n in ast.walk(annotation)):
            continue
        prefix = raw[target_end:offset(annotation)]
        if re.fullmatch(rb"\s*:(?:\s|\(|\#[^\r\n]*)*", prefix) is None:
            continue
        colon = target_end + prefix.index(b":")
        edits[(colon, colon + 1)] = b"\x00"
        # Direct credential literals remain redacted, but syntactically valid.
        # Empty/None/numeric control defaults and symbolic expressions are not
        # claimed to be recognizable credentials by this plaintext masker.
        if isinstance(value, ast.Constant) and isinstance(value.value, (str, bytes)) and value.value:
            replacement = _MASK.encode() if isinstance(value.value, bytes) else _MASK
            edits[(offset(value), offset(value, end=True))] = repr(replacement).encode()
    return [(start, end, value) for (start, end), value in edits.items()]


def _protect_request_annotations(text: str) -> tuple[str, bool]:
    """Bounded source-only exemption; ambiguous/malformed text keeps old masking.

    Collision-free NUL/SOH shield ONLY declaration separators. Values, comments
    and even annotation text still pass through both credential-pattern layers.
    The marker is restored only after those layers, never by restoring raw code.
    """
    if (len(text) > 131_072 or "\x00" in text or "\x01" in text or ":" not in text
            or re.search(r"(?m)^[ \t]*(?:async[ \t]+def|def|class)[ \t]+\w", text) is None):
        return text, False
    # Dedenting supports method excerpts. Keep original indentation through a
    # per-line UTF-8 offset map instead of rewriting the source being sent.
    def edits_for(region: str) -> list[tuple[int, int, bytes]]:
        source = textwrap.dedent(region)
        edits = _request_annotation_edits(source)
        original_lines = region.encode("utf-8").splitlines(keepends=True)
        source_lines = source.encode("utf-8").splitlines(keepends=True)
        mapping: list[int] = []
        position = 0
        for original, dedented in zip(original_lines, source_lines):
            removed = len(original) - len(dedented)
            mapping.extend(range(position + removed, position + len(original)))
            position += len(original)
        mapping.append(position)
        return [(mapping[start], mapping[end - 1] + 1, value) for start, end, value in edits]

    try:
        edits = edits_for(text)
    except (SyntaxError, ValueError, RecursionError, UnicodeError):
        # Prose and malformed source are expected input, not lost failures.
        # Never extract inner fences: they may be data inside an outer text
        # fence or an unterminated Python string. Keep conservative masking.
        return text, False
    if not edits:
        return text, False
    raw = text.encode("utf-8")
    for start, end, value in sorted(edits, reverse=True):
        raw = raw[:start] + value + raw[end:]
    return raw.decode("utf-8"), True


def mask_secrets_for_request(text: str, *, preserve_annotations: bool = True) -> str:
    """Credential-pattern masking for request content, not telemetry.

    Reuse both canonical pattern layers (including short prefixed keys), but
    exclude the log-only opaque >=40-character heuristic: source identifiers
    and hashes are legitimate model input. Unknown unlabelled opaque/encoded
    credentials cannot be identified by this function. Import/mask errors must
    be refused by the send boundary, never replaced with the original input.
    """
    from agi_v8_1.providers.base import _mask_secret

    prepared, annotations = _protect_request_annotations(text) if preserve_annotations else (text, False)
    if preserve_annotations and not annotations:
        prepared, annotations = _protect_serialized_request_annotations(text)
    masked = _mask_secret(mask_text(prepared), mask_opaque_tokens=False)
    return masked.replace("\x00", ":").replace("\x01", "=") if annotations else masked


def _protect_serialized_request_annotations(text: str) -> tuple[str, bool]:
    """Handle the canonical base-provider envelope at the final send pass.

    Native payload leaves were inspected before serialization; the final pass
    must inspect them too, not trust that earlier call or skip the envelope.
    Require a complete, canonically serialized JSON suffix, then parse whole
    string values as Python. Arbitrary quoted fragments/fences get no exemption.
    Only proven separators are shielded; envelope prose and data remain masked.
    """
    separator = "\n\nStructured payload:\n"
    if len(text) > 524_288 or any(marker in text for marker in ("\x00", "\x01", r"\u0000", r"\u0001")):
        return text, False
    prefix, found, payload = text.rpartition(separator)
    if not found:
        return text, False
    try:
        native = json.loads(payload)
        if type(native) is not dict or json.dumps(native, ensure_ascii=False, indent=2) != payload:
            return text, False
    except (ValueError, RecursionError):
        return text, False  # noncanonical/unparseable envelope keeps old masking
    # The marker is reproducible by an attacker, so repeat native key/schema
    # and structure checks rather than treating it as evidence of a prior pass.
    # Leaves are finite strings; errors propagate to the fail-closed boundary.
    from agi_v8_1.providers.egress import mask_request_content

    mask_request_content(native)
    # The JSON document has already been validated. Consuming each entire
    # string token avoids treating escaped quotes inside one value as framing.
    tokens = re.finditer(r'"(?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*"', payload)
    edits = []
    for token in tokens:
        if payload.startswith(":", token.end()):
            continue  # JSON keys are never annotation-exempted
        decoded = json.loads(token.group())
        protected, changed = _protect_request_annotations(decoded)
        if changed:
            # Original literal/encoded NUL is excluded above. Only our markers
            # become raw NUL here, and are restored after BOTH masking layers.
            encoded = (json.dumps(protected, ensure_ascii=False)
                       .replace(r"\u0000", "\x00").replace(r"\u0001", "\x01"))
            edits.append((token.start(), token.end(), encoded))
    for start, end, value in reversed(edits):
        payload = payload[:start] + value + payload[end:]
    return prefix + found + payload, bool(edits)


# __SLOT_MONEY_ROW_SURVIVES_MASKER_IMPORT_2026_08_09__ 🔴 돈행 증발 동종 결함.
#
# 위/아래 두 함수의 lazy ``providers.base`` import 가 **통째로** 실패하면(패키지
# 손상, providers 를 못 싣는 AST-제한 환경) 예외가 호출자
# ``enforcement.executor_log._emit`` 의 바깥 try 로 흘러 행이 0개 써졌다 —
# 2026-08-09 재현: meta_path 차단 → rows=0, cost_usd $1.23 이 원장에서 증발.
# ``__SLOT_RECORDER_OWES_NOBODY_2026_08_09__``(si_spend_ledger) 와 같은 부류인데,
# 이쪽은 판정 규칙을 옮겨올 수 없다: ``_mask_secret`` 의 정본은 providers.base 에
# 있고 여기 사본을 두면 검사 안 받는 쪽이 썩는다.
#
# ⇒ 마스킹은 안전 게이트라 fail-open(1차 마스킹만 하고 통과)은 금지다 — mask_text
# 는 짧은 접두 키(sk-{6,})를 놓친다. 대신 **내용을 이름 붙여 통째로 유보**한다:
# 문자열 내용은 행에 싣지 않고 실패의 이름만 싣는다. 돈 칸(cost_usd/ts/executor/…)
# 은 마스킹을 안 거치므로 행 자체는 살아남고, 실패는 ``_swallowed`` 로 세어지며
# (strict fail-fast 에서는 재발화) 행 위의 sentinel 로도 보인다 — 침묵이 아니다.
def _strict_masker_unavailable(exc: BaseException) -> str:
    """Named, fail-closed stand-in for string content when the strict
    second-pass masker cannot be imported.

    Carries ONLY the exception class name. The message is DROPPED, not
    ``mask_text``-ed: an import-failure message can itself quote a short
    prefixed key (``sk-{6,15}``) that only the unavailable second-pass masker
    would catch, so embedding ``mask_text(str(exc))`` here re-opened the very
    leak this sentinel exists to close (R1 적대검증 REFUTED, 2026-08-09 —
    sentinel 이 스스로 세운 '반쪽 마스킹 출력 금지'를 자기 몸으로 위반).
    The full message still reaches observability via ``_swallowed`` at the
    call sites (counted + named, re-raised under strict fail-fast) — it just
    never lands in a ledger row.
    """
    return (
        "<content withheld: strict masker unavailable "
        f"({type(exc).__name__}) — providers.base import failed; fail-closed, "
        "__SLOT_MONEY_ROW_SURVIVES_MASKER_IMPORT_2026_08_09__>"
    )


def _withhold_masked_obj(pre: Any, sentinel: str) -> Any:
    """Fail-closed object stand-in for :func:`mask_obj_strict`'s fallback.

    dict/list/tuple/set/frozenset/str/bytes 는 문자열이 숨을 수 있는 자리라
    **통째로 유보**한다(키 자리에도 짧은 키가 올 수 있어 구조 보존 대신 전면
    유보를 택했다). set/frozenset 분기는 R1 적대검증이 잡은 맹점의 수리
    (2026-08-09): 종전엔 ``return pre`` 로 원문이 통과했다. 컨테이너 타입
    계약은 유지한다(set→set, frozenset→frozenset — mask_obj 와 동일).
    숫자/불리언/``None`` 은 비밀을 담을 수 없어 그대로 통과한다.
    """
    if isinstance(pre, dict):
        return {"_strict_mask_unavailable": sentinel}
    if isinstance(pre, (list, tuple)):
        return [sentinel]
    if isinstance(pre, set):
        return {sentinel}
    if isinstance(pre, frozenset):
        return frozenset({sentinel})
    if isinstance(pre, str):
        return sentinel
    if isinstance(pre, (bytes, bytearray)):
        return sentinel
    return pre


# __SLOT_W1A2__: bytes/bytearray decoder for masking. Returns a masked str
# (decoded from the input bytes) and a flag of whether the underlying
# decode/mask actually changed the payload. Used by mask_obj for type-
# preserving return (bytes in -> bytes out) and by contains_secret to
# scan bytes payloads.
def _mask_bytes(data: bytes | bytearray) -> tuple[bytes | bytearray, bool]:
    """Decode bytes-like, run mask_text, re-encode. Best-effort UTF-8 + errors=replace.

    Returns (masked_bytes_like_same_type, changed_bool).
    Empty/None-shaped payloads pass through with changed=False.
    """
    if not isinstance(data, (bytes, bytearray)) or not data:
        return data, False
    try:
        decoded = bytes(data).decode("utf-8", errors="replace")
    except Exception as _ff_exc:
        # If decode fails (it shouldn't with errors='replace'), preserve.
        _swallowed(_ff_exc, site="policy.secret_masker._mask_bytes:247", category="config")
        return data, False
    masked_str = mask_text(decoded)
    changed = masked_str != decoded
    if not changed:
        return data, False
    try:
        encoded = masked_str.encode("utf-8", errors="replace")
    except Exception as _ff_exc:
        _swallowed(_ff_exc, site="policy.secret_masker._mask_bytes:256", category="config")
        return data, False
    if isinstance(data, bytearray):
        return bytearray(encoded), True
    return encoded, True


# Alias for v7.1 API compatibility
mask_secrets = mask_text


def _mask_leaf(obj: Any, *, secret_context: bool = False) -> Any:
    """Mask a single leaf value (delegates to mask_text for strings)."""
    if secret_context:
        if obj is None:
            return None
        return _MASK
    if isinstance(obj, str):
        return mask_text(obj)
    return obj


def mask_obj(
    obj: Any,
    _depth: int = 0,
    _seen: set | None = None,
    _secret_context: bool = False,
) -> Any:
    """Recursively mask secrets with R9.S7 depth + cycle guards.

    Returns a new structure; input is never mutated.

    Depth > _MAX_RECURSION_DEPTH yields a placeholder string rather than
    recursing further. Cycle detection by ``id()`` (frozen set per call so
    sibling containers don't pollute each other).

    Dict values whose key is structurally sensitive (``api_key``,
    ``password``, ``token``, etc.) are masked even when the value is short
    and has no standalone token shape.
    """
    if _seen is None:
        _seen = set()
    if _depth > _MAX_RECURSION_DEPTH:
        return "<MASKED:max_depth>"
    # __SLOT_W1A2__: include frozenset in the cycle-tracked container set.
    if isinstance(obj, (dict, list, tuple, set, frozenset)):
        oid = id(obj)
        if oid in _seen:
            return "<MASKED:cycle>"
        _seen = _seen | {oid}
    # __SLOT_W1A2__: bytes/bytearray leaf handling. If secret_context, return
    # an empty bytes/bytearray-typed mask sentinel so the downstream sink can
    # type-check. Otherwise decode-mask-reencode preserving the original type.
    if isinstance(obj, (bytes, bytearray)):
        if _secret_context:
            mask_bytes = _MASK.encode("utf-8")
            return bytearray(mask_bytes) if isinstance(obj, bytearray) else mask_bytes
        masked, _changed = _mask_bytes(obj)
        return masked
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        for i, (k, v) in enumerate(obj.items()):
            if i >= _MAX_CONTAINER_ITEMS:
                out["<MASKED:truncated>"] = True
                break
            # R9.1: keys can carry secrets too (Authorization: Bearer ...).
            masked_key = mask_obj(k, _depth + 1, _seen)
            value_secret_context = _secret_context or _is_secret_key(k)
            try:
                out[masked_key] = mask_obj(
                    v, _depth + 1, _seen, value_secret_context
                )
            except TypeError as _ff_exc:
                _swallowed(_ff_exc, site="policy.secret_masker.mask_obj:328", category="config")
                out[str(masked_key)] = mask_obj(
                    v, _depth + 1, _seen, value_secret_context
                )
        return out
    if isinstance(obj, list):
        return [
            mask_obj(v, _depth + 1, _seen, _secret_context)
            for v in obj[:_MAX_CONTAINER_ITEMS]
        ]
    if isinstance(obj, tuple):
        return tuple(
            mask_obj(v, _depth + 1, _seen, _secret_context)
            for v in obj[:_MAX_CONTAINER_ITEMS]
        )
    if isinstance(obj, set):
        items = list(obj)[:_MAX_CONTAINER_ITEMS]
        return {mask_obj(v, _depth + 1, _seen, _secret_context) for v in items}
    # __SLOT_W1A2__: frozenset handling — mirror set branch but preserve
    # the immutable frozenset return type so the original container's
    # type contract isn't downgraded by the masker.
    if isinstance(obj, frozenset):
        items = list(obj)[:_MAX_CONTAINER_ITEMS]
        return frozenset(
            mask_obj(v, _depth + 1, _seen, _secret_context) for v in items
        )
    return _mask_leaf(obj, secret_context=_secret_context)


def mask_payload(payload: Any) -> Any:
    """Top-level entry; alias for mask_obj."""
    return mask_obj(payload)


def mask_obj_strict(obj: Any) -> Any:
    """Composed STRUCTURED masker — the object analog of mask_secrets_strict.

    ``mask_obj`` masks string leaves with ``mask_text`` (the ``sk-{16,}``
    threshold) plus key-aware sensitive values, but — exactly like ``mask_text``
    on a bare string — it MISSES short prefixed keys (``sk-{6,}`` / ``xai-`` /
    ``AIza`` / ``Bearer ...``) embedded in a NESTED string value. A payload that
    carries arbitrary external rows (a trader outcome dict, an executor summary)
    can therefore leak a short key buried in a nested field.

    This runs ``mask_obj`` first (key-aware + long-content), then a second deep
    pass applying ``providers.base._mask_secret`` to every string leaf (and str
    key) across the same container set ``mask_obj`` walks — dict/list/tuple/
    set/frozenset, plus bytes via decode-mask-reencode — so a short key inside
    a str/bytes leaf of any nested container cannot survive.

    # __SLOT_OBJ_LEAF_STRINGIFY_MASK_2026_08_10__ BOUNDARY (say only what the
    # code does): non-string, non-container leaves — custom objects, Exception
    # instances — pass through BOTH passes UNCHANGED. A key riding such a
    # leaf's ``__str__``/``__repr__`` is invisible here because the leaking
    # string does not exist yet; it is minted later, at stringification
    # (``str()`` / ``json.dumps(default=...)``). Any consumer that stringifies
    # masked output MUST therefore re-mask the minted string (the ledger does:
    # ``enforcement.executor_log._mask_stringified``). It is the
    structured counterpart of ``mask_secrets_strict`` and the SINGLE source of
    truth for masking arbitrary nested payloads — callers must not re-implement
    the composition. ``providers.base`` is imported LAZILY (stdlib-only at
    import time, same discipline as ``mask_secrets_strict``); if that import
    fails outright, string-bearing content is WITHHELD behind a named sentinel
    (:func:`_withhold_masked_obj`) instead of propagating and eating the
    caller's row.
    """
    pre = mask_obj(obj)
    try:
        from agi_v8_1.providers.base import _mask_secret
    except Exception as exc:  # noqa: BLE001 — import 실패가 호출자의 돈 행을 먹으면 안 된다
        _swallowed(exc, site="policy.secret_masker.mask_obj_strict:import",
                   category="telemetry")
        return _withhold_masked_obj(pre, _strict_masker_unavailable(exc))

    # __SLOT_STRICT_MASK_COVERS_ALL_CONTAINERS_2026_08_09__ set/frozenset/bytes
    # 분기는 R1 적대검증이 잡은 기존 맹점의 수리: 종전 _deep 은 str/dict/list/
    # tuple 만 재귀하고 나머지는 ``return v`` — 1차 mask_obj(mask_text, sk-{16,})
    # 만 거친 짧은 키(sk-{6,15})가 set 잎/bytes 잎에서 두 마스커를 다 통과해
    # 디스크에 남았다. mask_obj 가 이미 순회하는 컨테이너 집합과 동일하게 맞춘다
    # (타입 계약도 동일: set→set, frozenset→frozenset, bytes→bytes).
    def _deep(v: Any) -> Any:
        if isinstance(v, str):
            return _mask_secret(v)
        if isinstance(v, dict):
            return {
                (_mask_secret(k) if isinstance(k, str) else k): _deep(x)
                for k, x in v.items()
            }
        if isinstance(v, list):
            return [_deep(x) for x in v]
        if isinstance(v, tuple):
            return tuple(_deep(x) for x in v)
        if isinstance(v, set):
            return {_deep(x) for x in v}
        if isinstance(v, frozenset):
            return frozenset(_deep(x) for x in v)
        if isinstance(v, (bytes, bytearray)):
            # _mask_bytes(1차, mask_text)와 같은 decode-mask-reencode 계약을
            # 2차 마스커로 반복한다 — bytes in -> bytes out, 변화 없으면 원본.
            decoded = bytes(v).decode("utf-8", errors="replace")
            masked = _mask_secret(decoded)
            if masked == decoded:
                return v
            encoded = masked.encode("utf-8", errors="replace")
            return bytearray(encoded) if isinstance(v, bytearray) else encoded
        return v

    return _deep(pre)


# --- the stringification boundary (SHARED — promoted 2026-08-10) ------------
# __SLOT_SHARED_STRINGIFY_MASK_2026_08_10__ 승격 이유(duplicate-prompt-definition
# 함정): ``mask_obj``/``mask_obj_strict`` 는 비문자열·비컨테이너 잎(커스텀 객체,
# Exception 인스턴스)을 무변화 통과시킨다 — 유출하는 문자열은 아직 존재하지도
# 않고, **나중에** 싱크가 ``str()`` 로 주조한다. 그래서 마스킹 층이 str() **뒤에**
# 한 번 더 서야 한다. 이 층은 2026-08-10 에 ``enforcement.executor_log`` 안에
# 사설로 태어났고(``_compose_secret_mask``/``_mask_stringified``/``_jsonify``),
# ``bus.falsifier_bus._jsonify`` 는 그 **옛 사본**("Same idiom as
# executor_log._jsonify")이라 같은 유출을 그대로 안고 있었다. 사본을 하나 더 뜨면
# 검사 안 받는 쪽이 썩는다 ⇒ 정본을 여기(마스킹의 단일 진실원)로 올린다.
#
# 계약(사본과 축자 동일해야 한다 — 동기화 앵커 테스트가 기계로 대조한다):
#   - JSON-native 잎(None/bool/int/float/str)은 **그대로** 통과. 이미 마스킹된
#     문자열을 재마스킹하지 않는다(byte-parity: 비밀 없는 행은 무변화).
#   - dict: str 키는 그대로, 비-str 키는 주조 후 마스킹. 값은 재귀.
#   - list/tuple/set/frozenset → list (JSON 에 set 이 없다).
#   - 그 외 모든 잎(callable/bytes/커스텀 객체/예외) → 주조 후 마스킹.
#   - 어떤 실패도 예외로 새 나가지 않는다(fail-closed): 내용은 이름 붙은
#     sentinel 로 유보하고 **행은 살린다** — 원장 싱크의 돈행 증발 금지 계약.
#
# ⚠️ 위 마지막 줄은 2026-08-10 R3 적대검증 시점에 ``jsonify_masked`` 에 대해
#    **거짓이었다**(실측: 자기참조 dict/list → ``RecursionError`` 전파). 형제
#    ``mask_obj`` 는 R9.S7 순환/깊이 가드를 갖는데, 승격하며 새로 공개한 이 함수만
#    안 갖고 있었다. 버스 경로에서는 도달 불가였지만(값이 ``mask_obj_strict_guarded``
#    를 먼저 통과해 순환이 ``<MASKED:cycle>`` 로 접힌다) — **정본의 계약 문구가
#    사실과 달랐다**. 다른 소비자가 그 문구를 믿고 부르면 행을 먹는다.
#    ⇒ 문구를 낮추지 않고 **코드를 계약에 맞췄다**: 아래 ``jsonify_masked`` 가
#      형제와 같은 어휘(``<MASKED:cycle>``/``<MASKED:max_depth>``)로 접는다.
#
# 🔴 B4 (2026-08-10, 판2 잔여 정직화) — R3 가 닫은 것은 순환·깊이 **둘뿐**이었고
#    그 사실 자체는 위에 정직하게 적혀 있었다("순환·깊이 가지만"). 실측(B4)으로
#    **세 번째 미닫힌 경로**를 잡았다: ``dict``/``list``/``set``/``frozenset`` 을
#    상속하고 순회 프로토콜(``.items()``/``__iter__``)이 던지는 컨테이너
#    (설정 로더·ORM row·원격 fetch 프록시 — ``mask_obj_strict_guarded`` 가 같은
#    입력 부류에서 이미 겪은 결함과 동종, 재현: ``RaisingMapping({}).items()``
#    → ``RuntimeError`` 가 ``jsonify_masked`` 밖으로 그대로 전파). 버스 경로는
#    이번에도 도달 불가다(같은 이유 — ``mask_obj_strict_guarded`` 가 먼저 그런
#    매핑을 유보 문자열로 접는다) 하지만 **정본 심볼을 직접 부르는 소비자**는
#    여전히 행을 먹는다. 이번엔 문구를 낮추지 않고 또 코드를 계약에 맞춘다 —
#    순회 자체를 try/except 로 감싸 실패 시 이름 붙은 ``<MASKED:...>`` 로 접는다
#    (행은 살고 원문은 안 실린다). 이 부류를 다 훑었다는 뜻은 아니다 — 다음에
#    또 미닫힌 경로가 나오면 이 계약 문구를 다시 재측정할 것.
#
# 🔴 B4 — 원장 행 바이트 결정성. ``set``/``frozenset`` → list 변환은 파이썬
#    반복 순서(``PYTHONHASHSEED`` 의존, 문자열 원소면 프로세스마다 다르다)를
#    그대로 물려받아 **event_id 는 결정론이어도 행 바이트는 비결정**이었다
#    (``bus.falsifier_bus._compute_event_id`` 는 별도 canon 층에서 이미 정렬하지만
#    그건 해시 기저일 뿐 저장되는 행 값이 아니다). 아래 ``jsonify_masked`` 가
#    set/frozenset 원소를 저장 직전 정렬해 고정한다 — 라이브 원장(156행, 2026-08-10
#    실측)에는 set/frozenset 값이 **한 건도 없어** 이 정규화는 오늘 byte-identical
#    이다(테스트로 고정: ``test_c1_busleak_pan2_r2_2026_08_10.py``).
_STRINGIFY_MASK_SLOT = "__SLOT_SHARED_STRINGIFY_MASK_2026_08_10__"

# __SLOT_SHARED_STRINGIFY_MASK_2026_08_10__ ``jsonify_masked`` 전용 깊이 상한.
# ⛔ ``_MAX_RECURSION_DEPTH``(=8) 를 재사용하지 않는다: 그건 **마스킹**의 상한이고,
# 이 함수는 이미 마스킹을 마친 구조를 JSON 으로 강제하는 후단이라 같은 8 을 쓰면
# 오늘 통과하던 행을 새로 잘라 byte-parity 를 깬다. 여기 상한의 목적은 의미가
# 아니라 **인터프리터 재귀 한계 회피** 하나뿐이라, ``mask_obj`` 가 내보낼 수 있는
# 최대 깊이(8+컨테이너 한 겹)보다 한참 위이면서 파이썬 기본 한계(≈1000, 레벨당
# 2프레임)보다 한참 아래인 값을 고른다 ⇒ 오늘의 출력은 무변화(테스트로 고정).
_JSONIFY_MAX_DEPTH = 64


# __SLOT_STRICT_IS_NOT_OURS_TO_BEND_2026_08_10__ ⚠️ 이 래퍼들의 "절대 던지지
# 않는다"는 **기본 모드(strict OFF)의 계약**이다. ``AGI_V8_STRICT_FAIL_FAST=true``
# 에서는 ``_swallowed`` 가 재발화하고, 그러면 이 래퍼도 던진다.
#
# 그게 옳다고 판정한 근거(2026-08-10, R2 적대검증 뒤 재결정):
#   (a) strict 는 default-OFF 디버그 모드다. 레포 어디에도 이 게이트를 켜는 코드
#       /설정이 없다(실측: ``.py/.md/.json`` 전수 grep — 켜는 자리 0, 문서·주석
#       ·goal card 뿐). OFF 에서는 이 층이 실제로 절대 안 던진다.
#   (b) "strict 에서도 안 던지는 층"을 만들려면 ``fail_fast.swallowed`` 에 면제
#       스위치를 심어야 하는데, 그건 **573 사이트가 물린 레포 전역 초크포인트**
#       이고 이 판의 소유 밖이다. 국소 결함(버스 유출)을 고치려고 전역 안전 장치의
#       의미를 바꾸는 것은 폭발반경이 지키려는 것보다 크다.
#   (c) 레포가 이미 반대 방향을 계약으로 못 박았다:
#       ``test_strict_mode_spares_no_category`` = strict 는 카테고리를 봐주지
#       않는다. "방어층만 봐준다"는 같은 규칙의 예외를 새로 뚫는 일이다.
#   (d) 그 재설계는 이미 **다른 사람 몫으로 기입돼 있다** —
#       ``goal_cards/drafts/gc-119_strict_fail_fast_green.json`` 이 "strict 에서
#       터지는 지점을 원인 수리하거나 계약상 비치명임을 구조로 증명"을 과업으로
#       선언한다. 판2 가 그 카드를 국소적으로 선취하면 안 된다.
# ⇒ 여기서는 strict 를 굽히지 않는다. strict ON 에서 마스킹이 터지면 크게 터지는
#    것이 이 레포의 의도된 동작이고, 그 사실을 테스트로 고정해 둔다.
def mask_secrets_strict_guarded(text: Any) -> str:
    """:func:`mask_secrets_strict` 의 fail-closed 래퍼 — strict OFF 에서 안 던진다.

    ``mask_secrets_strict`` 는 자기 lazy import 실패만 fail-close 한다. 그 밖의
    실패(병리적 ``__str__``, 정규식 폭발)는 그대로 던져 호출자의 원장 행을
    통째로 먹는다(돈행 증발, 2026-08-10 재현). 여기서 내용만 이름 붙여 유보하고
    행은 살린다 — 반쪽 마스킹도, 원문도 아니다.

    ⚠️ strict fail-fast ON 에서는 ``_swallowed`` 가 재발화하므로 이 래퍼도 던진다
    (위 ``__SLOT_STRICT_IS_NOT_OURS_TO_BEND_2026_08_10__`` 판정 근거 참조).
    """
    try:
        return mask_secrets_strict(text)
    except Exception as exc:  # noqa: BLE001 — 마스킹 실패가 호출자의 행을 먹으면 안 된다
        _swallowed(exc, site="policy.secret_masker.mask_secrets_strict_guarded",
                   category="telemetry")
        return (
            "<content withheld: composed mask failed "
            f"({type(exc).__name__}) — fail-closed, "
            f"{_STRINGIFY_MASK_SLOT}>"
        )


def mask_obj_strict_guarded(obj: Any) -> Any:
    """:func:`mask_obj_strict` 의 fail-closed 래퍼 — strict OFF 에서 안 던진다.

    구조 보존 대신 **전면 유보**(문자열 sentinel): 키 자리에도 짧은 키가 숨을 수
    있어 반쪽 보존은 안전하지 않다.

    # __SLOT_STRICT_IS_NOT_OURS_TO_BEND_2026_08_10__ 이 가드가 실제로 무엇을
    # 구하는지 **측정**했다(R2 가 "미측정"으로 남긴 항목). 종전 근거였던 "상태
    # 의존 ``__hash__``" 는 틀린 예시다 — ``__hash__`` 가 던지는 객체는 dict 를
    # **짓는 시점**에 이미 터져서 마스커에 도달하지 못한다(R2 실측). 실제로 이
    # 가드가 물리는 입력은 **``dict``/``list`` 를 상속하고 순회 프로토콜이 던지는
    # 매핑**이다: ``mask_obj`` 는 ``isinstance(obj, dict)`` 로 분기해 ``.items()``
    # 를 부르므로, lazy 매핑(설정 로더·ORM row·원격 fetch 프록시)이 그 자리에서
    # 던지면 예외가 ``append_event`` 바깥 try 로 흘러 **이벤트가 통째로 증발**한다.
    # 재현: ``tests/v8_1/test_c1_busleak_pan2_r2_2026_08_10.py::
    # test_mask_obj_guard_is_load_bearing_for_a_raising_mapping``.
    """
    try:
        return mask_obj_strict(obj)
    except Exception as exc:  # noqa: BLE001 — 마스킹 실패가 호출자의 행을 먹으면 안 된다
        _swallowed(exc, site="policy.secret_masker.mask_obj_strict_guarded",
                   category="telemetry")
        return (
            "<content withheld: object mask failed "
            f"({type(exc).__name__}) — fail-closed, "
            f"{_STRINGIFY_MASK_SLOT}>"
        )


# __SLOT_B4_STRINGIFY_ADDRESS_STABILITY_2026_08_10__ 🔴 R2(id 기저)가 이미 겪은
# 결함이 행 값 층에도 그대로 있었다: 기본 ``__repr__``/``__str__`` 은 객체
# **메모리 주소**를 물고 나온다(``<__main__.Leaf object at 0x7f...>``). 주소는
# 프로세스마다 다르므로, ``mask_stringified`` 가 그런 문자열을 그대로 실으면
# 같은 논리 이벤트의 원장 행 바이트가 프로세스마다 달라진다 — event_id 는
# ``bus.falsifier_bus._stabilize_addresses`` 로 이미 결정론인데(해시 기저
# 층), **행 값**은 그 층을 안 거쳤다(재현: fresh process ×3, 같은 3개짜리
# ``{Leaf(), Leaf(), Leaf()}`` prediction → event_id 는 동일, 저장된
# prediction 바이트는 매번 다름). ``bus.falsifier_bus._id_basis_leaf`` 와
# 사본을 또 뜨지 않으려고 **여기 하나**로 승격한다.
#
# __SLOT_B4_ID_BASIS_ROW_ANCHOR_PARITY_2026_08_10__ 🔴 위 문단이 원래 "id
# 기저 층은 여전히 자기 버전(더 넓은 앵커)을 쓴다 — 판별력 우선"이라고 적어
# 뒀던 것 자체가 재검증 major #2 의 재발 지점이었다: 두 층이 서로 다른 정규식을
# 쓰면 같은 address-bearing 잎에서 **한 층은 접고 다른 층은 안 접는** 비대칭이
# 생긴다(id 는 안정인데 행 바이트는 비결정 — 이 판이 다른 곳에서 이미 "자기
# 모순"이라 부른 바로 그 모양). 그래서 ``bus.falsifier_bus._stabilize_
# addresses`` 는 이제 **이 함수를 그대로 위임**한다(자기 정규식 없음, 문자
# 그대로 같은 앵커) — id 기저 층도 행 값 층과 같은 결정을 내린다. 그 결과
# 각괄호로 안 닫히는 커스텀 repr 은 id 기저에서도 더는 안 접힌다(판별력을
# 조금 더 지키는 방향이 아니라 **두 층의 일치**가 우선한 트레이드오프 — 자세한
# 근거는 ``bus/falsifier_bus.py`` 의 같은 슬롯 참조).
#
# 🔴 B4 판2 잔여 적대검증(2026-08-10) — 바로 위 "좁은 앵커... 내용에
# 우연히 낀 임의 hex 는 안 건든다"는 **거짓이었다**(major #2, 라이브
# 재현). 옛 앵커 ``(?<= at )0x[0-9a-fA-F]+`` 는 ``' at '`` 뒤 hex 라면
# 무엇이든 물어서 평범한 문장도 파괴했다:
#   str(RuntimeError('segfault at 0x00007fff in module'))
#     → 'segfault at 0xSTABLE in module'   (내용 파괴, 진단 정보 유실)
#   str(ValueError('invalid opcode at 0xdeadbeef'))
#     → 'invalid opcode at 0xSTABLE'
# 기본 object repr/바운드 메서드/function/generator 는 항상 ``... at
# 0x<hex>>`` 로 **주소 바로 뒤에 `>` 로 닫힌다**(``<모듈.클래스 object at
# 0x7f...>``, 바운드 메서드 repr 도 중첩 ``>>`` 로 같은 모양). 그 모양만
# 물도록 lookahead(``(?=>)`` — 주소와 `>` 사이 공백 없음)를 하나 더 좁힌다.
# 이제 정상 문장(주소 뒤에 `>` 가 없는 어떤 산문)은 무변화다(재현: 위 두
# 예시 모두 원문 그대로 통과, ``<Leaf object at 0x7f...>`` 는 여전히
# ``<Leaf object at 0xSTABLE>``).
#
# 🔴 타워 R4 major(2026-08-10) — "실제 객체 repr 만 안정화된다"는 예전 문구는
# **과장**이었다: CPython 기본 repr 이 전부 저 모양은 아니다. 실측(라이브
# 재현, 아래 anchor-precision 테스트가 고정):
#   ``repr(sys._getframe())``  -> ``<frame at 0x7f..., file '<string>', ...>``
#   ``repr(code_obj)``         -> ``<code object <module> at 0x7f..., file ...>``
#   ``repr(cell)``             -> ``<cell at 0x7f...: int object at 0x93a2f0>``
# 세 계열 다 주소 바로 뒤가 ``,``/``:`` 라 이 lookahead 에 안 걸린다 — **이
# 앵커의 방어는 frame/code/cell 객체 repr 에는 미치지 않는다.** 그런 객체가
# 어쩌다 원장 잎(prediction/outcome/tags)에 실리면 그 필드의 행 바이트는
# 여전히 프로세스 간 비결정이다(정직 고지, 방어력 손실이 아니라 애초에
# 없었던 방어를 있다고 과장한 것 — 아래에서 정정).
#
# 왜 넓히지 않는가: ``,``/``:`` 까지 lookahead 에 더하면 major #2 가 이미
# 재현한 프로세형 문장("Access violation at 0x00401234, terminating" 류
# 진단 메시지가 주소 뒤에 쉼표를 흔히 단다)을 다시 파괴할 위험이 실측
# 근거보다 크다 — 이 판이 스스로 세운 "판별력보다 결정성이 우선하되
# 평범한 산문은 파괴하지 않는다"는 우선순위 안에서, frame/code/cell 은
# 원장 잎으로 실제 등장한 라이브 사례가 0건이라(census) 그 트레이드오프를
# 감수하지 않는다. 다음에 이 계열이 라이브에서 실측되면 재고할 것.
_STRINGIFY_ADDRESS = re.compile(r"(?<= at )0x[0-9a-fA-F]+(?=>)")
_STRINGIFY_ADDRESS_STABLE = "0xSTABLE"


def stabilize_object_repr_addresses(text: str) -> str:
    """기본 ``__repr__``/``__str__`` 이 물고 나오는 객체 메모리 주소를 고정
    토큰으로 접는다 — ``mask_stringified`` 가 주조한 문자열을 프로세스 간
    결정론으로 만들기 위한 좁은 정규화.

    앵커는 CPython 기본 repr 의 정확한 모양(``... at 0x<hex>>`` — 주소
    바로 뒤에 여닫는 ``>``)에 걸린다: ``(?<= at )0x[0-9a-fA-F]+(?=>)``.
    그래서 ``"segfault at 0x00007fff in module"`` 처럼 ``>`` 로 안 닫히는
    평범한 산문은 무변화이고(2026-08-10 B4, major #2 정정 — 종전 앵커는
    이 문장도 먹었다), ``<Leaf object at 0x7f...>``/``<function f at
    0x7f...>``/바운드 메서드 repr 은 안정화된다.

    🔴 **범위 정정(2026-08-10 R4, 과장 철회)**: "실제 객체 repr 은 전부
    안정화된다"는 예전 문구는 거짓이었다 — ``frame``/``code``/``cell``
    객체의 기본 repr 은 주소 바로 뒤가 ``,``/``:`` 라 이 앵커에 안 걸린다
    (``<frame at 0x7f..., file ...>`` 는 무변화로 통과, 주소가 그대로
    남는다). 이 세 계열의 잎이 원장에 실리면 그 필드의 저장 바이트는
    여전히 프로세스 간 비결정이다 — 자세한 실측/트레이드오프 근거는 위
    ``_STRINGIFY_ADDRESS`` 컴파일 직전 주석(__SLOT_B4_STRINGIFY_ADDRESS
    _STABILITY_2026_08_10__ 뒤 R4 단락) 참조. 오늘 라이브 원장에 그런 잎이
    실린 사례는 0건(census)이라 실피해는 없지만, "안정화된다"는 서술은
    **주소 바로 뒤 `>` 로 닫히는 repr 계열**로 한정해서 읽어야 한다.

    ⚠️ 트레이드오프: 완전히 동일한 기본 repr 형태(클래스명 + 주소만 다름)인
    서로 다른 인스턴스는 주소를 접으면 서로 구분 불가가 된다. 이건
    ``bus.falsifier_bus._id_basis_leaf`` 가 해시 기저 층에서 이미 감수한
    것과 같은 트레이드오프다 — 안전 게이트에서는 판별력보다 결정성이 우선.

    ⚠️ 잔여(정직 고지, B4): 커스텀 ``__repr__``/``__str__`` 이 **자기
    안에서** 비결정 순서의 컨테이너(예: ``set``)를 문자열로 조립해 넣는
    경우(가령 ``f"Holder(tags={self._tags})"`` — ``_tags`` 가 ``set``)는
    이 함수의 대상 밖이다. 이미 완성된 문자열 하나를 받을 뿐 그 안에 어떤
    컨테이너가 어떻게 조립됐는지는 모른다 — 이건 이 마스킹 층이 아니라
    호출자 코드의 ``__repr__`` 구현이 결정성을 스스로 챙겨야 하는 자리다
    (구조적으로 여기서 닫을 수 없다).
    """
    return _STRINGIFY_ADDRESS.sub(_STRINGIFY_ADDRESS_STABLE, text)


def mask_stringified(value: Any) -> str:
    """비-JSON-native 잎을 주조(``str()``)한 **뒤** 그 문자열을 마스킹한다.

    ``str()`` 자체가 터지는 잎은 종전 싱크 계약 그대로 ``<unserializable>``.
    (이 문자열은 사본과 축자 동일 — 동기화 앵커 테스트가 대조한다.)

    B4(2026-08-10): 주조된 문자열은 마스킹 **전에** 먼저
    :func:`stabilize_object_repr_addresses` 를 거친다 — 주소가 없는 문자열은
    무변화(byte-parity), 주소가 있는 문자열만 프로세스 간 결정론이 된다.
    """
    try:
        text = str(value)
    except Exception as _ff_exc:  # noqa: BLE001 — 병리적 ``__str__`` 은 실재한다
        # 이 폴백(``<unserializable>``)이 strict OFF 에서의 계약이고, 두 사본
        # (executor_log/secret_masker)의 동기화 앵커가 축자 대조하는 값이다.
        _swallowed(_ff_exc, site="policy.secret_masker.mask_stringified",
                   category="telemetry")
        return "<unserializable>"
    text = stabilize_object_repr_addresses(text)
    return mask_secrets_strict_guarded(text)


def _mask_type_name(value: Any) -> str:
    """``type(value).__name__`` 을 마스킹해서 돌려준다.

    # __SLOT_B4_SENTINEL_TYPE_NAME_MASKED_2026_08_10__ ``jsonify_masked`` 가
    # 순회-실패 sentinel(``<MASKED:iteration_failed:{name}>``)에 싣는 타입
    # 이름은 그 자체로 주조면이다 — 동적으로 지어진 클래스(``type("sk-live-...",
    # (dict,), ...)``, ORM row 프록시의 파생 클래스명 등)는 이름 자리에 시크릿
    # 모양을 담을 수 있다(실측: ORM/lazy-mapping 팩토리 패턴). 이름 자체를
    # ``mask_secrets_strict_guarded`` 로 한 번 더 걸러 sentinel 이 원문을
    # 우회 착지시키지 못하게 한다. 이름 조회 자체가 실패하는 경우는 없지만
    # (``type()`` 은 항상 성공) 방어적으로 폴백을 둔다.
    #
    # 🔴 B4 판2 잔여 적대검증(2026-08-10) — 이 함수는 종전 ``stabilize_
    # object_repr_addresses`` 를 안 거쳤는데, ``enforcement.executor_log``
    # 의 동종 자리(``_jsonify`` 의 순회-실패 sentinel)는 ``_mask_stringified``
    # 를 거쳐 그 정규화가 **항상** 걸린다 — 이름이 ``"X at 0xdeadbeef"`` 처럼
    # 우연히 주소-모양 부분 문자열을 담으면 두 사본이 서로 다른 sentinel을
    # 냈다(사본 갈라짐, minor 재현: xlog=``...X at 0xSTABLE``, 여기=
    # ``...X at 0xdeadbeef``). 같은 정규화 단계를 밟아 동기화한다 — 위에서
    # 앵커를 ``(?=>)`` 로 좁혔으므로 이 정규화는 실제 CPython 기본 repr
    # 모양(``... at 0x<hex>>``)에서만 발동하고 평범한 클래스명은 무변화다.
    """
    try:
        name = type(value).__name__
    except Exception as _ff_exc:  # noqa: BLE001 — 이론상 도달 불가, 방어적
        _swallowed(_ff_exc, site="policy.secret_masker._mask_type_name",
                   category="telemetry")
        name = "<unknown>"
    return mask_secrets_strict_guarded(stabilize_object_repr_addresses(name))


# __SLOT_B4_SORT_KEY_IS_FAIL_CLOSED_2026_08_10__ 🔴 B4 재검증 major — 신규
# 회귀. set/frozenset 원소를 저장 직전 정렬하려고 넣은 정렬 키가
# ``json.dumps(v, sort_keys=True, ...)`` 였는데, ``sort_keys=True`` 는 dict
# 값의 키를 ``sorted(dct.items())`` 로 **비교**한다 — v 가 (str 서브클래스처럼
# ``isinstance(k, str)`` 를 통과해 원형 그대로 dict 키 자리에 남는) 병리적
# ``__lt__``(무조건 raise) 를 가진 키를 ≥2개 담은 dict 면 그 비교가 그대로
# 터진다. 재현: ``class BadKey(str): __lt__ = raise TypeError`` 를 키로 쓰는
# dict 를 set 원소로 넣으면 ``jsonify_masked`` 가 정렬 키 계산 **자체**에서
# TypeError 를 밖으로 흘렸다 — 이 함수가 스스로 세운 "어떤 실패도 예외로
# 안 샌다" 계약의 새 위반이었다(이번 라운드가 만든 정렬 자체가 그 구멍이다).
#
# 폴백 사다리: (1) sort_keys=True — 오늘의 정상 출력과 byte-identical(그대로
# 통과하는 입력은 무변화). (2) 실패하면 sort_keys=False — dict 키 **비교**가
# 없어 위 실패 원인을 우회한다(대부분의 병리적 키는 여기서 풀린다. 내용은
# 여전히 반영되므로 판별력도 대체로 보존된다). (3) 그것도 실패하면(이론상
# 도달 불가 — ``sort_keys=False`` 는 키 비교를 안 하므로 ``default=str`` 이
# str() 까지 터뜨리는 극단적 병리에서만) 타입 이름만. 마지막 폴백에서는 같은
# 타입의 서로 다른 두 값이 정렬 순서로 구분되지 않는다(정직 고지) — 그래도
# ``sort()`` 자체는 절대 던지지 않는다.
# __SLOT_B4_DICT_INSERT_IS_LOSSLESS_2026_08_10__ 🔴 B4 판2 잔여 적대검증 major
# (2026-08-10, 라이브 재현) — ``jsonify_masked``/``executor_log._jsonify`` 의
# dict 분기는 ``out[key] = value`` 를 무가드로 불렀다. 두 실패 양식이 실측됐다:
#
#   (1) 조용한 행 증발(row-internal): 서로 다른 두 non-str 키가
#       ``mask_stringified``(주소 안정화)를 거치며 **같은 문자열로 접힐 수 있다**
#       (예: 기본 repr 만 다른 두 인스턴스 → 둘 다 ``<...object at 0xSTABLE>``).
#       마지막 쓰기가 이기고 먼저 쓴 값은 **키가 아니라 값이** 조용히 사라진다 —
#       행은 살지만 원소 하나가 없어진다. 순환/과깊이 sentinel 과 달리 이름도
#       안 붙고 ``_swallowed`` 도 안 센다.
#   (2) 예외 전파: ``isinstance(k, str)`` 을 통과한 str 서브클래스가 병리적
#       ``__hash__``/``__eq__`` 를 가지면(마스킹 대상이 아니다 — 이미 str 이라
#       그대로 키 자리에 남는다) ``key in out`` / ``out[key] = …`` 자체가
#       ``TypeError``/``ValueError`` 를 던져 이 함수가 스스로 세운 "어떤 실패도
#       예외로 안 샌다" 계약을 어겼다.
#
# 수리: 삽입을 전용 헬퍼로 옮기고 fail-closed 로 만든다.
#   - 키 조회/삽입이 던지면(포이즌드 해시/eq) 위치 기반 sentinel 키로 대체한다
#     (``idx`` 는 ``items()`` 순회 인덱스 — 같은 입력이면 항상 같은 값, 프로세스
#     간 결정론).
#   - 정규화 후 키가 **이미 존재**하면(충돌) 절대 덮어쓰지 않는다 — ``#2``, ``#3``
#     … 로 순번을 붙여 두 원소 다 살린다. 정상 입력(오늘의 78종 라이브 id 포함
#     — 문자열 키뿐이라 충돌이 구조적으로 불가능)에서는 이 분기에 닿지 않아
#     byte-identical 이다.
def _dict_insert_no_loss(out: dict[str, Any], key: Any, value: Any, idx: int) -> None:
    """``out[key] = value`` 의 fail-closed·무손실 버전.

    ``jsonify_masked``/``executor_log._jsonify`` 의 dict 분기가 공유한다
    (executor_log 는 이 심볼을 그대로 임포트 — 사본 아님).
    """
    try:
        exists = key in out
    except Exception as exc:  # noqa: BLE001 — 포이즌드 __hash__/__eq__ 는 실재한다
        _swallowed(exc, site="policy.secret_masker._dict_insert_no_loss:lookup",
                   category="telemetry")
        key = f"<MASKED:unhashable_key:{idx}>"
        exists = key in out
    if exists:
        # __SLOT_B4_R4_COLLISION_IS_COUNTED_2026_08_10__ 🔴 타워 R5 재정정 — 키
        # 자체는 ``#2`` 순번으로 disambiguate 돼 행에서 살아남지만(위 major
        # 수리), 그 충돌이 벌어졌다는 사실 자체는 그때까지 어떤 관측 표면에도
        # 안 남았다(row 를 직접 diff 해야만 보였다). 이 함수의 다른 모든 실패
        # 분기가 ``_swallowed`` 로 세는데 이 분기만 조용했다 — 같은 규율로
        # 맞춘다. ⚠️ 여기서 세는 값은 "행에서 이미 살아남은 충돌"이다(데이터는
        # 안 잃었다) — 위 lookup/insert 분기의 "정말 실패했다"와는 다른 사실
        # 이라 구분되는 site 를 쓴다(미측정 vs 측정된-충돌을 섞지 않는다).
        # 🔴 R5 적대검증: 위 논리에서 `_swallowed` 를 곧이곧대로 부른 게 회귀
        # 였다 — HEAD 는 이 분기 자체가 없어(주소 안정화 전에는 충돌이 구조적
        # 으로 불가능) STRICT 에서 아예 안 죽었는데, `_swallowed` 는 strict
        # 에서 무조건 되던지므로 **이미 성공적으로 해소된 충돌**이 caller
        # crash + 돈 행 증발로 변한다(executor_log 가 이 심볼을 그대로 쓴다).
        # 이 분기는 실패를 잡은 게 아니라 성공을 세고 싶을 뿐이다 — 카운터는
        # 살리고 되던짐만 여기서 국소 흡수한다(B1 daemon_ready 와 같은 모양,
        # 침묵 래칫에 이유 붙여 계정).
        try:
            _swallowed(
                KeyError(f"masked key collision disambiguated: {key!r}"),
                site="policy.secret_masker._dict_insert_no_loss:collision",
                category="telemetry",
            )
        except Exception:
            pass  # strict 재던짐 국소 흡수 — 위 주석 참조. 카운터는 이미 반영됨
        n = 2
        candidate = f"{key}#{n}"
        while candidate in out:
            n += 1
            candidate = f"{key}#{n}"
        key = candidate
    try:
        out[key] = value
    except Exception as exc:  # noqa: BLE001 — 방어적(위 조회가 이미 대부분을 거른다)
        _swallowed(exc, site="policy.secret_masker._dict_insert_no_loss:insert",
                   category="telemetry")
        out[f"<MASKED:unhashable_key:{idx}>#{len(out)}"] = value


def _row_sort_key(v: Any) -> str:
    """set/frozenset 원소를 저장 직전 정렬하기 위한 결정론적 문자열 키.

    fail-closed: 어떤 입력에도 예외를 밖으로 내지 않는다(위
    ``__SLOT_B4_SORT_KEY_IS_FAIL_CLOSED_2026_08_10__`` 참조).
    """
    try:
        return json.dumps(v, sort_keys=True, ensure_ascii=False, default=str)
    except Exception as exc:  # noqa: BLE001 — 정렬 키 생성이 행을 먹으면 안 된다
        _swallowed(exc, site="policy.secret_masker._row_sort_key:sort_keys",
                   category="telemetry")
        try:
            return json.dumps(v, sort_keys=False, ensure_ascii=False, default=str)
        except Exception as exc2:  # noqa: BLE001 — 이론상 도달 불가, 방어적
            _swallowed(exc2, site="policy.secret_masker._row_sort_key:no_sort_keys",
                       category="telemetry")
            return f"<unsortable:{type(v).__name__}>"


def jsonify_masked(value: Any, _depth: int = 0, _seen: frozenset | None = None) -> Any:
    """(이미 마스킹된) 구조를 JSON-safe 잎으로 강제하며, **주조하는 모든 문자열**을
    다시 마스킹한다.

    ``json.dumps`` 에 ``default=`` 가 없는 싱크(``state.store.atomic_append_jsonl``)
    가 낯선 잎에서 터지지 않게 문자열화하되, 그 문자열화가 바로 마스킹을 우회하는
    유출면이라 같은 자리에 마스킹 층을 세운다. 이미 문자열인 잎은 재마스킹하지
    않는다(byte-parity).

    순환/과깊이 방어(2026-08-10 R3): 자기참조 컨테이너는 ``mask_obj`` 와 **같은
    어휘**로 접는다(``<MASKED:cycle>`` / ``<MASKED:max_depth>``). ``_seen`` 은
    호출마다 새로 만드는 frozenset 이라 형제 컨테이너가 서로를 오염시키지 않는다
    (같은 객체가 두 가지에 각각 나타나는 것은 순환이 아니다).

    순회 실패 방어(2026-08-10 B4, R3 뒤 한 단계 더 좁혀 닫음): ``dict``/
    ``list``/``set``/``frozenset`` 을 상속하고 순회 프로토콜이 던지는 컨테이너는
    ``<MASKED:iteration_failed:...>`` 로 접는다 — 이것도 예외로 새 나가지 않는다.
    ``dict`` 는 두 자리에서 던질 수 있다: ``.items()`` 호출 자체, 그리고 그
    결과를 ``for k, v in items`` 로 **언패킹**하는 자리(items() 가 2-튜플이
    아닌 원소를 내는 경우 — 실측: ``def items(self): return [("a",1,2)]`` /
    ``return [42]``). 둘 다 같은 sentinel 로 접는다. sentinel 에 실리는
    타입 이름 자체도 마스킹한다 — 동적으로 시크릿을 이름에 담은 클래스
    (``type("sk-live-...", (dict,), ...)``)가 sentinel 을 통해 원문을 실어
    나르지 못하게.

    행 바이트 결정성(2026-08-10 B4): ``set``/``frozenset`` 원소는 저장 직전
    정렬한다 — 반복 순서가 ``PYTHONHASHSEED`` 의존이라 정렬 없이는 같은 논리
    이벤트가 프로세스마다 다른 바이트열로 착지했다(event_id 는 별도 canon 층이
    이미 정렬하지만 그건 행 값이 아니라 해시 기저다). 객체 잎의 기본 repr 이
    물고 나오는 주소도 :func:`mask_stringified` 가
    :func:`stabilize_object_repr_addresses` 로 접으므로, 정렬 키 자체도
    프로세스 간 안정적이다.

    ⚠️ 정직 고지: 위 방어들은 **오늘 실측된** 실패 양식(라이브 순회 예외 재현,
    라이브 원장 0건의 set 값)에 대한 것이다. "어떤 실패도 예외로 안 샌다"는
    계약은 R3 이후 이 함수에 대해 네 번 거짓으로 잡혔다(순환/깊이, 순회 호출
    실패, 순회 언패킹 실패, 그리고 아래) — 다음에 또 미닫힌 경로가 나오면 이
    문구를 또 재측정할 것이지 "이제 완전하다"로 승격하지 않는다. 알려진
    잔여(의도적 미방어):
    - ``BaseException`` 서브클래스(``KeyboardInterrupt`` 등)를 던지는 순회는
      안 잡는다(``except Exception`` 만 — 레포 전역 관례, 신호 억제 방지).
    - **2026-08-10 B4 R5 실측**: 위 첫 줄 ``isinstance(value, (bool, int,
      float, str))`` 자체가 ``value.__class__`` 를 내부에서 읽는다 —
      ``__class__`` 가 예외를 던지는 property 인 병리적 객체가 오면 그
      ``isinstance`` 호출에서 곧바로 이 함수 **밖으로** 예외가 새 나간다
      (``enforcement.executor_log._jsonify`` 사본도 동일 — 두 카피가 같은
      ``isinstance`` 관용구를 공유한다). HEAD 도 이미 이랬으므로 회귀는
      아니다. 이 가족을 닫으려면 재귀 트리 각 층의 형(type) 판별 자체를
      감싸야 해 이 함수의 성능·가독 트레이드오프가 크다 — 오늘 라이브
      호출부에 이런 병리적 잎을 만드는 생산자가 0건(census)이라 미룬다.
    """
    if _depth > _JSONIFY_MAX_DEPTH:
        return "<MASKED:max_depth>"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (dict, list, tuple, set, frozenset)):
        if _seen is None:
            _seen = frozenset()
        oid = id(value)
        if oid in _seen:
            return "<MASKED:cycle>"
        _seen = _seen | {oid}
    if isinstance(value, dict):
        # __SLOT_SHARED_STRINGIFY_MASK_2026_08_10__ B4: ``dict`` 를 상속하고
        # ``.items()`` 가 던지는 lazy 매핑(설정 로더/ORM row/원격 fetch 프록시)은
        # ``isinstance`` 체크는 통과하지만 순회에서 터진다 — ``mask_obj_strict_
        # guarded`` 가 이미 같은 부류에서 겪은 결함과 동종(재현: R2 텍스트 참조).
        # fail-closed: 순회 자체가 실패면 컨테이너 전체를 이름 붙은 sentinel 로
        # 유보한다(행은 살리되, 반쪽 구조 보존은 하지 않는다 — 키 자리에도 짧은
        # 키가 숨을 수 있어서 전면 유보가 안전하다).
        try:
            items = list(value.items())
        except Exception as _ff_exc:  # noqa: BLE001 — 순회 프로토콜이 던지는 매핑
            _swallowed(_ff_exc, site="policy.secret_masker.jsonify_masked:dict_items",
                       category="telemetry")
            return f"<MASKED:iteration_failed:{_mask_type_name(value)}>"
        out: dict[str, Any] = {}
        for idx, item in enumerate(items):
            # __SLOT_B4_ITERATION_UNPACK_GUARD_2026_08_10__ ``.items()`` 호출
            # 자체는 안 던져도, 그 결과를 ``(k, v)`` 로 언패킹하는 이 자리가
            # 던질 수 있다(3-튜플/스칼라 원소 — 실측: verifier repro). 같은
            # sentinel 로 접는다.
            try:
                k, v = item
            except Exception as _ff_exc:  # noqa: BLE001 — 2-튜플이 아닌 원소
                _swallowed(_ff_exc, site="policy.secret_masker.jsonify_masked:dict_unpack",
                           category="telemetry")
                return f"<MASKED:iteration_failed:{_mask_type_name(value)}>"
            # __SLOT_B4_STR_SUBCLASS_KEY_IS_NOT_PLAIN_STR_2026_08_10__ ``isinstance``
            # 는 str **서브클래스**도 통과시켜, 포이즌드 ``__hash__``/``__eq__`` 를
            # 가진 키(설정 로더/ORM row 가 흔히 내는 모양)가 무변화로 키 자리에
            # 남았다(재검증 major #3, 라이브 재현: 해싱/비교에서 TypeError/
            # ValueError 전파). ``type(k) is str`` 로 좁혀 서브클래스는 전부
            # ``mask_stringified`` 를 거치게 한다 — ``str(subclass_instance)`` 는
            # (``__str__`` 를 안 덮어쓴 한) 포이즌드 dunder 를 안 건드리고 순수
            # ``str`` 을 낸다(실측). 부수 이득: 서브클래스 키에 숨은 시크릿도
            # 이제 마스킹된다(종전엔 무변화라 마스킹을 건너뛰었다).
            key = k if type(k) is str else mask_stringified(k)
            # __SLOT_B4_DICT_INSERT_IS_LOSSLESS_2026_08_10__ 무가드 ``out[key] =``
            # 는 키 충돌(원소 증발) / 포이즌드 str-서브클래스 키(예외 전파) 둘 다
            # 냈다 — 전용 fail-closed 헬퍼로 삽입한다.
            _dict_insert_no_loss(out, key, jsonify_masked(v, _depth + 1, _seen), idx)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        # __SLOT_SHARED_STRINGIFY_MASK_2026_08_10__ B4: 같은 fail-closed 순회
        # 방어(list/tuple/set/frozenset 을 상속하고 ``__iter__`` 가 던지는 경우).
        try:
            items = list(value)
        except Exception as _ff_exc:  # noqa: BLE001
            _swallowed(_ff_exc, site="policy.secret_masker.jsonify_masked:iter",
                       category="telemetry")
            return f"<MASKED:iteration_failed:{_mask_type_name(value)}>"
        masked_items = [jsonify_masked(v, _depth + 1, _seen) for v in items]
        if isinstance(value, (set, frozenset)):
            # __SLOT_SHARED_STRINGIFY_MASK_2026_08_10__ B4: 원소 반복 순서는
            # ``PYTHONHASHSEED`` 에 걸린다(문자열/바이트 원소일 때 프로세스마다
            # 다르다) — event_id 는 별도 canon 층(``falsifier_bus._id_basis_canon``)
            # 에서 이미 정렬되지만, 그건 해시 기저일 뿐 **저장되는 행 값이 아니다**.
            # 원장 행 바이트도 결정론이려면 여기서 저장 직전에 정렬해야 한다.
            # 정렬 키는 이미 JSON-native 로 접힌 ``masked_items`` 의 직렬화 문자열
            # (문자열/숫자/리스트/딕트가 섞여도 비교 가능하고, 마스킹 결과가 같으면
            # 항상 같은 순서를 낸다). fail-closed 폴백은 ``_row_sort_key`` 참조
            # (__SLOT_B4_SORT_KEY_IS_FAIL_CLOSED_2026_08_10__).
            masked_items.sort(key=_row_sort_key)
        return masked_items
    # 그 외(callable, bytes, 커스텀 객체, 예외) -> 주조 + 마스킹.
    return mask_stringified(value)


def contains_secret(
    obj: Any, _depth: int = 0, _seen: set | None = None
) -> bool:
    """True iff masking (``mask_obj``) would change the payload's leaves anywhere.

    R9.1 invariant: depth clamp returns True (treat unverified payload as
    secret-bearing) rather than the fail-open False.

    # __SLOT_OBJ_LEAF_STRINGIFY_MASK_2026_08_10__ BOUNDARY: non-string,
    # non-container leaves (custom objects, Exception instances) answer False
    # even when their ``__str__`` carries a key — consistent with ``mask_obj``,
    # which passes such leaves through unchanged (this predicate's contract is
    # "would mask_obj change it", and its consumers retention_decider /
    # privacy_policy use it PAIRED with ``mask_payload``). The leak surface is
    # the later stringification boundary, and the closure lives there
    # (``executor_log._mask_stringified``); a str()-scan here would need the
    # strict composition (``_mask_secret``'s {40,} catch-all) and would flip
    # retention for hash/uuid-bearing payloads — a behavior change beyond
    # masking, gated work, deliberately NOT done in C1 R3.
    """
    if _seen is None:
        _seen = set()
    if _depth > _MAX_RECURSION_DEPTH:
        # Conservative: unverified payload counts as secret-bearing.
        return True
    # __SLOT_W1A2__: include frozenset in cycle-tracked container set.
    if isinstance(obj, (dict, list, tuple, set, frozenset)):
        oid = id(obj)
        if oid in _seen:
            return False
        _seen = _seen | {oid}
    if isinstance(obj, str):
        return mask_text(obj) != obj
    # __SLOT_W1A2__: bytes/bytearray must be decoded and scanned. The prior
    # implementation fell through to `return False` (fail-open) for bytes
    # which contradicted the R9.1 conservative invariant.
    if isinstance(obj, (bytes, bytearray)):
        _masked, changed = _mask_bytes(obj)
        return changed
    if isinstance(obj, dict):
        for i, (k, v) in enumerate(obj.items()):
            if i >= _MAX_CONTAINER_ITEMS:
                return True
            if _is_secret_key(k):
                return True
            if contains_secret(k, _depth + 1, _seen):
                return True
            if contains_secret(v, _depth + 1, _seen):
                return True
        return False
    # __SLOT_W1A2__: include frozenset in the list/tuple/set iterable check.
    if isinstance(obj, (list, tuple, set, frozenset)):
        for i, v in enumerate(obj):
            if i >= _MAX_CONTAINER_ITEMS:
                return True
            if contains_secret(v, _depth + 1, _seen):
                return True
        return False
    return False


__all__ = [
    "mask_text",
    "mask_secrets_strict",
    "mask_secrets_for_request",
    "mask_secrets",
    "mask_obj",
    "mask_obj_strict",
    "mask_payload",
    "contains_secret",
    # __SLOT_SHARED_STRINGIFY_MASK_2026_08_10__ 문자열화 경계 정본.
    "mask_secrets_strict_guarded",
    "mask_obj_strict_guarded",
    "mask_stringified",
    "jsonify_masked",
    "stabilize_object_repr_addresses",
]
