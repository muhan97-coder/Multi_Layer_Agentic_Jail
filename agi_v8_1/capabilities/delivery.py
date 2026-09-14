"""Bounded selected-public-skeleton delivery, not release or unlock authority.

Only reviewed source coordinates and newly authored synthetic smoke assets are
copied. Gate ownership stays at readers; PAYLOAD_SOURCES stays a separate
capability inventory. This does not complete Stage C or a T0--T2 release.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat

from agi_v8_1.capabilities.bundles import payload_source_paths


SCHEMA = "agi_v8_1.selected_skeleton_delivery.v1"
SCOPE = "selected-public-skeleton-v1"
MANIFEST_NAME = "delivery-manifest.json"
_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_TOTAL_BYTES = 16 * 1024 * 1024

# Existing cold-copy proof's exact reviewed closure. No source-tree discovery,
# import-derived authorization, Git dependency, or automatic transitive expansion.
PUBLIC_SOURCE_PATHS = (
    "__init__.py",
    "agent_system/__init__.py",
    "agent_system/swarm_v8/__init__.py",
    "agent_system/swarm_v8/config.py",
    "agent_system/swarm_v8/router_freeze.py",
    "agent_system/swarm_v8/schemas.py",
    "bridge/__init__.py",
    "bridge/change_detect_wire.py",
    "bridge/si_evidence.py",
    "bus/__init__.py",
    "bus/cycle_wire.py",
    "bus/falsifier_bus.py",
    "capabilities/__init__.py",
    "capabilities/bundles.py",
    "capabilities/declaration.py",
    "capabilities/ports.py",
    "core/__init__.py",
    "core/acceptance_gate.py",
    "core/axis_scorer.py",
    "core/continuation_ring.py",
    "core/cycle_artifacts.py",
    "core/cycle_policy_v81.py",
    "core/evidence_gate.py",
    "core/isolation_backend.py",
    "core/judge.py",
    "core/messages.py",
    "core/orchestrator_schema.py",
    "core/prompt_topology.py",
    "core/retry_chain.py",
    "core/sandbox_runner.py",
    "enforcement/__init__.py",
    "enforcement/acceptance_full.py",
    "enforcement/apply_chain_full.py",
    "enforcement/cost_limiter.py",
    "enforcement/destructive_command_policy.py",
    "enforcement/executor_command_policy.py",
    "enforcement/executor_integrity.py",
    "enforcement/executor_log.py",
    "enforcement/judge_full.py",
    "enforcement/safe_auto_apply.py",
    "memory/__init__.py",
    "memory/schema.py",
    "multimodal/__init__.py",
    "multimodal/dispatcher.py",
    "multimodal/evidence.py",
    "multimodal/schema.py",
    "multimodal/si_cycle_wire.py",
    "multimodal/task.py",
    "observability/__init__.py",
    "observability/coverage_meter.py",
    "observability/dashboard_stub.py",
    "observability/metric_log.py",
    "observability/test_output_parser.py",
    "orchestrator_v8.py",
    "policy/__init__.py",
    "policy/execution_authority.py",
    "policy/fail_fast.py",
    "policy/privacy_policy.py",
    "policy/raw_media_policy.py",
    "policy/retention_decider.py",
    "policy/secret_masker.py",
    "prompts/__init__.py",
    "prompts/loader.py",
    "prompts/prompt_compactor_v8.py",
    "providers/__init__.py",
    "providers/base.py",
    "providers/mock.py",
    "providers/plugins.py",
    "runtime/__init__.py",
    "runtime/card_targets.py",
    "runtime/episode_log.py",
    "runtime/episode_tee.py",
    "runtime/halt_sentinel.py",
    "runtime/ledger_join.py",
    "runtime/multi_orchestrator.py",
    "runtime/objective_patch_mode.py",
    "runtime/progress_stall.py",
    "runtime/workspace_snapshot.py",
    "self_improvement_v8.py",
    "si_lanes/__init__.py",
    "si_lanes/answer_product.py",
    "si_lanes/breadth_select.py",
    "si_lanes/command_channel.py",
    "si_lanes/objective_proposer.py",
    "si_lanes/outcome_observer.py",
    "si_lanes/promote.py",
    "si_lanes/proposal_archive.py",
    "si_lanes/proposer.py",
    "si_lanes/rollback_guard.py",
    "si_lanes/ticket_writer.py",
    "si_lanes/work_product.py",
    "state/__init__.py",
    "state/path_guard.py",
    "state/store.py",
    "utils/__init__.py",
    "utils/artifact_checks.py",
    "utils/debug_log.py",
    "utils/exceptions.py",
    "utils/failure_logger.py",
    "utils/hashing.py",
    "utils/io_helpers.py",
    "utils/time_ids.py",
    "verifier/wire_cross_model_v81.py",
)


# These are newly authored smoke assets, not private operating configuration,
# proprietary prompt packs, internal DOCS, or personal memory data.
PUBLIC_ASSET_PATHS = (
    ("config", "delivery_assets/public_stub/config.json"),
    ("prompt", "delivery_assets/public_stub/prompts/strategist.md"),
    ("docs", "delivery_assets/public_stub/README.md"),
)


@dataclass(frozen=True)
class DeliveryEntry:
    kind: str
    source: str
    destination: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class DeliveryManifest:
    entries: tuple[DeliveryEntry, ...]
    schema: str = SCHEMA
    scope: str = SCOPE


class DeliveryError(ValueError):
    """Fixed refusal code only; never expose file contents or OS diagnostics."""


def _coordinates():
    return tuple(("source", path, path) for path in PUBLIC_SOURCE_PATHS) + tuple(
        (kind, path, path) for kind, path in PUBLIC_ASSET_PATHS
    )


def _relative(value):
    if type(value) is not str or not value or "\\" in value:
        raise DeliveryError("invalid_relative_path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts) or PurePosixPath(value).is_absolute():
        raise DeliveryError("invalid_relative_path")
    return parts


def _validate_manifest(manifest):
    if (type(manifest) is not DeliveryManifest or manifest.schema != SCHEMA
            or manifest.scope != SCOPE or type(manifest.entries) is not tuple):
        raise DeliveryError("invalid_manifest")
    coordinates = _coordinates()
    if not manifest.entries or len(manifest.entries) != len(coordinates):
        raise DeliveryError("manifest_coordinates")
    seen = set()
    total = 0
    for entry, expected in zip(manifest.entries, coordinates):
        if type(entry) is not DeliveryEntry:
            raise DeliveryError("invalid_manifest_entry")
        _relative(entry.source)
        _relative(entry.destination)
        if entry.destination in seen:
            raise DeliveryError("duplicate_destination")
        seen.add(entry.destination)
        if (entry.kind, entry.source, entry.destination) != expected:
            raise DeliveryError("manifest_coordinates")
        if (type(entry.sha256) is not str or len(entry.sha256) != 64
                or any(char not in "0123456789abcdef" for char in entry.sha256)):
            raise DeliveryError("invalid_digest")
        if (type(entry.size_bytes) is not int or not 0 <= entry.size_bytes <= _MAX_FILE_BYTES):
            raise DeliveryError("invalid_size")
        total += entry.size_bytes
    if total > _MAX_TOTAL_BYTES:
        raise DeliveryError("bundle_too_large")
    if seen & payload_source_paths():
        raise DeliveryError("payload_in_public_skeleton")


def _open_root(path):
    """Walk absolute directory components with no-follow held descriptors."""
    absolute = Path(os.path.abspath(os.fspath(path)))
    fd = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in absolute.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except OSError:
        os.close(fd)
        raise DeliveryError("directory_unavailable") from None


def _open_directory(root_fd, parts, *, create=False):
    fd = os.dup(root_fd)
    try:
        for part in parts:
            # This is only an existence hint inside our new destination.
            # The no-follow directory open remains authoritative; races or
            # permission ambiguity raise rather than accepting another entry.
            if create and not os.access(part, os.F_OK, dir_fd=fd, follow_symlinks=False):
                os.mkdir(part, 0o755, dir_fd=fd)
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except OSError:
        os.close(fd)
        raise DeliveryError("directory_unavailable") from None


def _read_regular(root_fd, relative):
    parts = _relative(relative)
    parent = _open_directory(root_fd, parts[:-1])
    fd = None
    try:
        # NONBLOCK means a malicious FIFO cannot hang before the regular-file check.
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_FILE_BYTES:
            raise DeliveryError("invalid_source_file")
        chunks = []
        remaining = before.st_size + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(fd)
        if (len(data) != before.st_size
                or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise DeliveryError("source_changed_during_read")
        return data
    except OSError:
        raise DeliveryError("source_unavailable") from None
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def _manifest_bytes(manifest):
    return (json.dumps(asdict(manifest), ensure_ascii=True, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _matches(data, entry):
    if len(data) != entry.size_bytes or hashlib.sha256(data).hexdigest() != entry.sha256:
        raise DeliveryError("content_mismatch")


def build_manifest(source_root):
    """Hash only fixed source/asset paths. Missing assets never become fallbacks."""
    root_fd = _open_root(source_root)
    try:
        entries = []
        total = 0
        for kind, source, destination in _coordinates():
            data = _read_regular(root_fd, source)
            total += len(data)
            if total > _MAX_TOTAL_BYTES:
                raise DeliveryError("bundle_too_large")
            entries.append(DeliveryEntry(kind, source, destination,
                                         hashlib.sha256(data).hexdigest(), len(data)))
        manifest = DeliveryManifest(tuple(entries))
        _validate_manifest(manifest)
        return manifest
    finally:
        os.close(root_fd)


def _write_regular(root_fd, relative, data):
    parts = _relative(relative)
    parent = _open_directory(root_fd, parts[:-1], create=True)
    fd = None
    try:
        fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o644, dir_fd=parent)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise DeliveryError("destination_write_failed")
            view = view[written:]
    except OSError:
        raise DeliveryError("destination_write_failed") from None
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def _validate_inventory(root_fd, manifest):
    """List only expected directories; reject unknown children before descent."""
    children = {"": set()}
    paths = [entry.destination for entry in manifest.entries] + [MANIFEST_NAME]
    for path in paths:
        parts = _relative(path)
        for index, part in enumerate(parts):
            parent = "/".join(parts[:index])
            children.setdefault(parent, set()).add(part)
            if index < len(parts) - 1:
                children.setdefault("/".join(parts[:index + 1]), set())
    for directory, expected in sorted(children.items()):
        fd = _open_directory(root_fd, directory.split("/") if directory else [])
        try:
            if set(os.listdir(fd)) != expected:
                raise DeliveryError("unexpected_delivery_entry")
        except OSError:
            raise DeliveryError("inventory_unavailable") from None
        finally:
            os.close(fd)


def validate_bundle(bundle_root, manifest):
    """Prove fixed manifest, exact file set, current bytes, and payload absence."""
    _validate_manifest(manifest)
    fd = _open_root(bundle_root)
    try:
        # A repo path without this exact delivery marker fails before enumeration.
        if _read_regular(fd, MANIFEST_NAME) != _manifest_bytes(manifest):
            raise DeliveryError("manifest_content_mismatch")
        _validate_inventory(fd, manifest)
        for entry in manifest.entries:
            _matches(_read_regular(fd, entry.destination), entry)
    finally:
        os.close(fd)


def copy_bundle(source_root, destination_root, manifest):
    """Create a NEW local test candidate; never merge, publish, or overwrite.

    All approved source bytes are rechecked before the destination is created.
    Failure leaves no successful return; an interrupted partial destination must
    be discarded by its owner, never treated as validated delivery.
    """
    _validate_manifest(manifest)
    source = Path(os.path.abspath(os.fspath(source_root)))
    destination = Path(os.path.abspath(os.fspath(destination_root)))
    if destination == source or destination.is_relative_to(source):
        raise DeliveryError("destination_inside_source")
    source_fd = _open_root(source)
    try:
        data = {}
        for entry in manifest.entries:
            content = _read_regular(source_fd, entry.source)
            _matches(content, entry)
            data[entry.destination] = content
    finally:
        os.close(source_fd)
    parent = _open_root(destination.parent)
    fd = None
    try:
        os.mkdir(destination.name, 0o755, dir_fd=parent)
        fd = os.open(destination.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                     dir_fd=parent)
        for relative, content in data.items():
            _write_regular(fd, relative, content)
        _write_regular(fd, MANIFEST_NAME, _manifest_bytes(manifest))
        _validate_inventory(fd, manifest)
        for entry in manifest.entries:
            _matches(_read_regular(fd, entry.destination), entry)
    except OSError:
        raise DeliveryError("destination_unavailable") from None
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)
    validate_bundle(destination, manifest)
    return destination
