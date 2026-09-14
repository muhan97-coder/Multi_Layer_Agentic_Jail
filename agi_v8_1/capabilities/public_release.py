"""Explicit free-tier delivery candidate; neither release approval nor unlock.

The historical 106-file selected-skeleton scope is unchanged. Import discovery
reports gaps only: it never grants permission to ship another source or asset.
Personal state, operator environment, credentials and optional payload are absent.
"""
from __future__ import annotations

import ast
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path

from agi_v8_1.capabilities import delivery as _io
from agi_v8_1.capabilities.bundles import payload_source_paths


SCHEMA = "agi_v8_1.free_tier_delivery.v1"
SCOPE = "free-tier-coldstart-candidate-v1"
MANIFEST_NAME = _io.MANIFEST_NAME
MAX_TOTAL_BYTES = 16 * 1024 * 1024
_HISTORICAL_SOURCE_DIGEST = "09243c0f3f544c0201092540ca2d21c7b4782953d830e597cf96934ce1a1d638"

# Reviewed additions, not an import-driven or repository-wide export rule.
# Missing future integration files make build_manifest fail closed.
PUBLIC_SOURCE_PATHS = tuple(sorted(set(_io.PUBLIC_SOURCE_PATHS) | {
    "capabilities/delivery.py", "capabilities/public_release.py",
    "core/activation.py", "core/cycle_logger.py",
    "memory/distilled_contract.py", "memory/external_tmi.py",
    "policy/tier_gate.py", "policy/tier_token.py",
    "runtime/public_entry.py", "runtime/cost_reconcile.py",
    "runtime/daily_cost_cap.py", "runtime/episode_ctx.py",
    "runtime/episode_ledger.py", "runtime/spend_reservation.py",
    "runtime/tick_runner.py", "runtime/tick_policy_ports.py",
    "policy/gate_invariants.py", "runtime/cycle_memory.py",
    "runtime/repeated_failure_gate.py", "runtime/tick_auth.py",
    "runtime/campaign_registry.py",
    "runtime/tick_deadline.py", "runtime/tick_lease.py", "runtime/tick_descendant_seal.py",
    "tools/__init__.py", "tools/base.py", "tools/env_check.py",
    "tools/gen_inventory.py", "tools/gates_map.py", "tools/ledger_join_check.py",
    "tools/ratchet.py", "tools/public_coldstart_check.py", "tools/unlock.py",
    "verifier/cross_model_budget.py",
}))
PUBLIC_ASSET_PATHS = (
    ("config", "public_assets/free_tiers/config.json"),
    ("prompt", "public_assets/free_tiers/prompts/strategist.md"),
    ("docs", "public_assets/free_tiers/README.md"),
    ("curriculum", "public_assets/curriculum/local_tiers.json"),
)


class ReleaseError(ValueError):
    """A fixed error code; OS exceptions and source contents are never echoed."""


@dataclass(frozen=True)
class ReleaseManifest:
    entries: tuple[_io.DeliveryEntry, ...]
    schema: str = SCHEMA
    scope: str = SCOPE


def _coordinates():
    if hashlib.sha256("\n".join(_io.PUBLIC_SOURCE_PATHS).encode()).hexdigest() != _HISTORICAL_SOURCE_DIGEST:
        raise ReleaseError("historical_source_scope_changed")
    return tuple(("source", p, p) for p in PUBLIC_SOURCE_PATHS) + tuple(
        (kind, p, p) for kind, p in PUBLIC_ASSET_PATHS
    )


def _manifest_bytes(manifest):
    return (json.dumps(asdict(manifest), sort_keys=True, ensure_ascii=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def manifest_digest(manifest):
    validate_manifest(manifest)
    return hashlib.sha256(_manifest_bytes(manifest)).hexdigest()


def validate_manifest(manifest):
    if (type(manifest) is not ReleaseManifest or manifest.schema != SCHEMA
            or manifest.scope != SCOPE or type(manifest.entries) is not tuple):
        raise ReleaseError("invalid_manifest")
    coordinates = _coordinates()
    if not manifest.entries or len(manifest.entries) != len(coordinates):
        raise ReleaseError("manifest_coordinates")
    total, seen = 0, set()
    for entry, coordinate in zip(manifest.entries, coordinates):
        if type(entry) is not _io.DeliveryEntry:
            raise ReleaseError("invalid_entry")
        if (entry.kind, entry.source, entry.destination) != coordinate:
            raise ReleaseError("manifest_coordinates")
        if entry.destination in seen:
            raise ReleaseError("duplicate_destination")
        seen.add(entry.destination)
        if (type(entry.sha256) is not str or len(entry.sha256) != 64
                or any(c not in "0123456789abcdef" for c in entry.sha256)):
            raise ReleaseError("invalid_digest")
        if (type(entry.size_bytes) is not int
                or not 0 <= entry.size_bytes <= 2 * 1024 * 1024):
            raise ReleaseError("invalid_size")
        total += entry.size_bytes
    if total > MAX_TOTAL_BYTES:
        raise ReleaseError("bundle_too_large")
    if seen & payload_source_paths():
        raise ReleaseError("payload_in_public_bundle")


def build_manifest(source_root):
    fd = _io._open_root(source_root)
    try:
        entries = []
        for kind, source, destination in _coordinates():
            data = _io._read_regular(fd, source)
            entries.append(_io.DeliveryEntry(kind, source, destination,
                                             hashlib.sha256(data).hexdigest(), len(data)))
        manifest = ReleaseManifest(tuple(entries))
        validate_manifest(manifest)
        return manifest
    finally:
        os.close(fd)


def validate_bundle(root, manifest):
    validate_manifest(manifest)
    fd = _io._open_root(root)
    try:
        if _io._read_regular(fd, MANIFEST_NAME) != _manifest_bytes(manifest):
            raise ReleaseError("manifest_content_mismatch")
        _io._validate_inventory(fd, manifest)
        for entry in manifest.entries:
            _io._matches(_io._read_regular(fd, entry.destination), entry)
    finally:
        os.close(fd)


def copy_bundle(source_root, destination_root, manifest):
    """Copy a fixed, already hashed bundle into a new directory, never merge."""
    validate_manifest(manifest)
    source = Path(os.path.abspath(os.fspath(source_root)))
    destination = Path(os.path.abspath(os.fspath(destination_root)))
    if destination == source or destination.is_relative_to(source):
        raise ReleaseError("destination_inside_source")
    source_fd = _io._open_root(source)
    try:
        contents = {}
        for entry in manifest.entries:
            data = _io._read_regular(source_fd, entry.source)
            _io._matches(data, entry)
            contents[entry.destination] = data
    finally:
        os.close(source_fd)
    parent_fd, destination_fd = _io._open_root(destination.parent), None
    try:
        os.mkdir(destination.name, 0o755, dir_fd=parent_fd)
        destination_fd = os.open(destination.name, os.O_RDONLY | os.O_DIRECTORY
                                 | os.O_NOFOLLOW, dir_fd=parent_fd)
        for relative, data in contents.items():
            _io._write_regular(destination_fd, relative, data)
        _io._write_regular(destination_fd, MANIFEST_NAME, _manifest_bytes(manifest))
        _io._validate_inventory(destination_fd, manifest)
    except OSError:
        raise ReleaseError("destination_unavailable") from None
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        os.close(parent_fd)
    validate_bundle(destination, manifest)
    return destination


def _module_path(module, known):
    prefix = "agi_v8_1"
    if module != prefix and not module.startswith(prefix + "."):
        return None
    relative = module[len(prefix):].lstrip(".").replace(".", "/")
    candidates = ([relative + ".py", relative + "/__init__.py"]
                  if relative else ["__init__.py"])
    return next((p for p in candidates if p in known), candidates[0])


def closure_report(source_root):
    """Bounded AST diagnostics over approved paths, without reading discoveries.

    Eager imports must close. Deferred gaps are separately reported, not silently
    declared covered by the free-tier probes. Optional-code ports remain optional.
    This diagnostic cannot prove arbitrary dynamic imports or all upper tiers.
    """
    known = frozenset(PUBLIC_SOURCE_PATHS)
    payload = payload_source_paths()
    eager, deferred, payload_edges, parse_errors = set(), set(), set(), []
    fd = _io._open_root(source_root)
    try:
        trees = {}
        for relative in PUBLIC_SOURCE_PATHS:
            try:
                trees[relative] = ast.parse(_io._read_regular(fd, relative), filename=relative)
            except (SyntaxError, ValueError, UnicodeError):
                parse_errors.append(relative)
        # Parse each approved source once. Discoveries never enter this map.
        package_exports = {}
        for path, node in trees.items():
            if path == "__init__.py" or path.endswith("/__init__.py"):
                names = set()
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        names.add(child.name)
                    if isinstance(child, (ast.Import, ast.ImportFrom)):
                        names.update(a.asname or a.name.split(".")[0] for a in child.names)
                    if isinstance(child, ast.Assign):
                        names.update(t.id for t in child.targets if isinstance(t, ast.Name))
                package_exports[path] = names
        for relative, tree in trees.items():
            package = "agi_v8_1." + relative.replace("/", ".").removesuffix(".py")
            package = package.removesuffix(".__init__")
            if not relative.endswith("/__init__.py") and relative != "__init__.py":
                package = package.rsplit(".", 1)[0]
            def walk(node, delayed=False):
                delayed = delayed or isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                                       ast.Lambda))
                modules = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if node.level:
                        parts = package.split(".")
                        module = ".".join(parts[:len(parts) - node.level + 1]
                                          + ([module] if module else []))
                    modules = [module]
                    base = _module_path(module, known)
                    if base and base.endswith("__init__.py"):
                        # A lowercase module import from a package is an edge;
                        # symbols explicitly exported by that package are not.
                        exported = package_exports.get(base, set())
                        modules += [module + "." + a.name for a in node.names
                                    if a.name not in exported and not a.name.startswith("_")
                                    and a.name.islower() and a.name != "*"]
                for module in modules:
                    target = _module_path(module, known | payload)
                    if target is not None and target not in known:
                        edge = (relative, target)
                        if target in payload:
                            payload_edges.add(edge)
                        (deferred if delayed else eager).add(edge)
                for child in ast.iter_child_nodes(node):
                    walk(child, delayed)
            walk(tree)
    finally:
        os.close(fd)
    rows = lambda pairs: [{"source": a, "requires": b} for a, b in sorted(pairs)]
    return {"eager_closed": not eager and not parse_errors,
            "complete_static_closure": not eager and not deferred and not parse_errors,
            "eager_gaps": rows(eager), "deferred_gaps": rows(deferred),
            "payload_edges": rows(payload_edges), "parse_errors": sorted(parse_errors),
            "authorized_new_paths": []}
