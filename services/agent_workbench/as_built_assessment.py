"""As-Built Assessment evidence pack and cache helpers."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess  # nosec B404
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from orchestrator_agent.agent_tools.as_built_assessor.schemes import (
    ASSESSMENT_SCHEMA_VERSION,
    EVIDENCE_PACK_BUILDER_VERSION,
    EVIDENCE_PACK_SCHEMA_VERSION,
    AsBuiltAssessment,
    AsBuiltAssessmentCacheMeta,
    AuthorityTarget,
    EvidenceKind,
    EvidencePack,
    EvidenceSnippet,
    EvidenceWarning,
    RepoSnapshot,
    SearchObservation,
    SpecMode,
)
from services.agent_workbench.fingerprints import canonical_hash, canonical_json

if TYPE_CHECKING:
    from pathlib import Path

AS_BUILT_ASSESS_COMMAND: str = "agileforge as-built assess"
AS_BUILT_ASSESSMENT_STATE_KEY: str = "as_built_assessment_cached"
AS_BUILT_ASSESSMENT_META_STATE_KEY: str = "as_built_assessment_cache_meta"
MAX_SCAN_BYTES: int = 500 * 1024
MAX_AUTHORITY_TARGETS: int = 150
MAX_SNIPPETS_PER_TARGET: int = 5
MAX_SNIPPET_LINES: int = 40
MAX_SNIPPET_BYTES: int = 8 * 1024
MAX_PACK_BYTES: int = 750 * 1024
MAX_FILE_MANIFEST_ENTRIES: int = 300
GIT_BINARY: str = shutil.which("git") or "git"

_SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        ".codegraph",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "venv",
    }
)
_SKIP_FILE_NAMES: frozenset[str] = frozenset(
    {"uv.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock"}
)
_SKIP_SUFFIXES: frozenset[str] = frozenset(
    {
        ".db",
        ".sqlite",
        ".sqlite3",
        ".lock",
        ".pyc",
        ".pyo",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".pdf",
        ".zip",
        ".gz",
    }
)
_DOC_DIR_NAMES: frozenset[str] = frozenset({"doc", "docs", "documentation"})
_DOC_SUFFIXES: frozenset[str] = frozenset({".md", ".mdx", ".rst", ".txt"})
_CONFIG_NAMES: frozenset[str] = frozenset(
    {
        ".env.example",
        "pyproject.toml",
        "ruff.toml",
        "mypy.ini",
        "pytest.ini",
        "package.json",
        "tsconfig.json",
    }
)
_CONFIG_SUFFIXES: frozenset[str] = frozenset({".toml", ".yaml", ".yml"})
_TEST_SUFFIXES: tuple[str, ...] = (
    ".test.js",
    ".spec.js",
    ".test.ts",
    ".spec.ts",
    ".test.tsx",
    ".spec.tsx",
)
_ID_TERM_RE: re.Pattern[str] = re.compile(
    r"^(INV-[A-Za-z0-9_-]+|REQ\.[A-Za-z0-9_.-]+|QUALITY\.[A-Za-z0-9_.-]+|"
    r"CONSTRAINT\.[A-Za-z0-9_.-]+|INTERFACE\.[A-Za-z0-9_.-]+|"
    r"DATA\.[A-Za-z0-9_.-]+)$"
)


def utc_now_iso() -> str:
    """Return canonical UTC timestamp."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def assessment_fingerprint(assessment: AsBuiltAssessment) -> str:
    """Return a canonical fingerprint for an assessment."""
    return canonical_hash(assessment.model_dump(mode="json"))


def cache_meta_for_assessment(
    assessment: AsBuiltAssessment,
) -> AsBuiltAssessmentCacheMeta:
    """Build workflow-state cache metadata for an assessment."""
    return AsBuiltAssessmentCacheMeta(
        schema_version=ASSESSMENT_SCHEMA_VERSION,
        agent_version=assessment.agent_version,
        evidence_pack_builder_version=assessment.evidence_pack_builder_version,
        authority_fingerprint=assessment.authority_fingerprint,
        repo_git_commit=assessment.repo_snapshot.git_commit,
        repo_dirty=assessment.repo_snapshot.dirty,
        evidence_pack_fingerprint=assessment.evidence_pack_fingerprint,
        assessment_fingerprint=assessment_fingerprint(assessment),
        generated_at=assessment.generated_at,
    )


def cached_assessment_for_backlog(state: dict[str, Any]) -> str:
    """Return cached assessment JSON for backlog input when internally fresh."""
    raw_assessment = state.get(AS_BUILT_ASSESSMENT_STATE_KEY)
    raw_meta = state.get(AS_BUILT_ASSESSMENT_META_STATE_KEY)
    if not isinstance(raw_assessment, str) or not isinstance(raw_meta, dict):
        return "NO_AS_BUILT_ASSESSMENT"
    try:
        assessment = AsBuiltAssessment.model_validate_json(raw_assessment)
        meta = AsBuiltAssessmentCacheMeta.model_validate(raw_meta)
    except ValueError:
        return "NO_AS_BUILT_ASSESSMENT"
    if meta.evidence_pack_builder_version != EVIDENCE_PACK_BUILDER_VERSION:
        return "NO_AS_BUILT_ASSESSMENT"
    if meta.assessment_fingerprint != assessment_fingerprint(assessment):
        return "NO_AS_BUILT_ASSESSMENT"
    if (
        meta.agent_version != assessment.agent_version
        or meta.evidence_pack_builder_version
        != assessment.evidence_pack_builder_version
        or meta.authority_fingerprint != assessment.authority_fingerprint
        or meta.repo_git_commit != assessment.repo_snapshot.git_commit
        or meta.repo_dirty != assessment.repo_snapshot.dirty
        or meta.evidence_pack_fingerprint != assessment.evidence_pack_fingerprint
    ):
        return "NO_AS_BUILT_ASSESSMENT"
    return canonical_json(assessment.model_dump(mode="json"))


def build_authority_targets(
    compiled: dict[str, Any],
) -> tuple[list[AuthorityTarget], list[EvidenceWarning], list[str]]:
    """Extract assessment targets from accepted authority."""
    warnings: list[EvidenceWarning] = []
    limitations: list[str] = []
    targets = _targets_from_invariants(compiled)
    if not targets:
        targets = _targets_from_items(compiled)

    if len(targets) > MAX_AUTHORITY_TARGETS:
        warnings.append(
            EvidenceWarning(
                code="AS_BUILT_AUTHORITY_TRUNCATED",
                message="Authority target list exceeded the Phase 1 cap.",
                details={
                    "target_count": len(targets),
                    "max_authority_targets": MAX_AUTHORITY_TARGETS,
                },
            )
        )
        targets = targets[:MAX_AUTHORITY_TARGETS]

    if not targets:
        warnings.append(
            EvidenceWarning(
                code="AS_BUILT_NO_AUTHORITY_TARGETS",
                message="No authority targets were extracted.",
                details={
                    "target_sources": [
                        "compiled_authority.invariants[]",
                        "compiled_authority.items[]",
                    ]
                },
            )
        )
        limitations.append("No authority targets were extracted.")

    return targets, warnings, limitations


def build_evidence_pack(  # noqa: PLR0913
    *,
    project_id: int,
    authority_fingerprint: str,
    compiled_authority: dict[str, Any],
    repo_path: Path,
    spec_mode: SpecMode,
    spec_file: Path | None,
) -> EvidencePack:
    """Build a bounded host-side evidence pack for the assessment agent."""
    repo = repo_path.resolve()
    if not repo.exists() or not repo.is_dir():
        msg = "repo path is not a readable directory"
        raise ValueError(msg)

    targets, target_warnings, limitations = build_authority_targets(
        compiled_authority
    )
    snapshot = _repo_snapshot(repo)
    files, skipped_counts = _scannable_files(repo)
    source_snippets, test_snippets, doc_snippets, search_observations = (
        _collect_target_evidence(
            files=files,
            targets=targets,
            skipped_counts=skipped_counts,
        )
    )

    warnings = [*target_warnings]
    if snapshot.dirty:
        warnings.append(
            EvidenceWarning(
                code="AS_BUILT_REPO_DIRTY",
                message="Repository has uncommitted changes.",
                details={"repo_path": snapshot.path},
            )
        )
    if skipped_counts:
        warnings.append(
            EvidenceWarning(
                code="AS_BUILT_SKIPPED_PATHS",
                message="Some repository paths were skipped during bounded scanning.",
                details={"counts": skipped_counts},
            )
        )

    summary = _manifest_summary(
        files=files,
        skipped_counts=skipped_counts,
        spec_file=spec_file,
        spec_mode=spec_mode,
        project_id=project_id,
    )
    pack = EvidencePack(
        schema_version=EVIDENCE_PACK_SCHEMA_VERSION,
        builder_version=EVIDENCE_PACK_BUILDER_VERSION,
        authority_fingerprint=authority_fingerprint,
        evidence_pack_fingerprint="sha256:pending",
        generated_at=utc_now_iso(),
        repo_snapshot=snapshot,
        warnings=warnings,
        file_manifest_summary=summary,
        authority_targets=targets,
        source_snippets=list(source_snippets.values()),
        test_snippets=list(test_snippets.values()),
        doc_snippets=list(doc_snippets.values()),
        cli_observations=[],
        search_observations=search_observations,
        limitations=limitations,
    )
    return _finalize_pack(pack)


def _collect_target_evidence(
    *,
    files: list[tuple[Path, Path, EvidenceKind]],
    targets: list[AuthorityTarget],
    skipped_counts: dict[str, int],
) -> tuple[
    dict[str, EvidenceSnippet],
    dict[str, EvidenceSnippet],
    dict[str, EvidenceSnippet],
    list[SearchObservation],
]:
    snippet_buckets: dict[str, dict[str, EvidenceSnippet]] = {
        "source": {},
        "test": {},
        "doc": {},
    }
    search_observations: list[SearchObservation] = []

    for target in targets:
        target_matches = 0
        matched_paths: list[str] = []
        target_snippet_count = 0
        for file_path, relative_path, kind in files:
            try:
                text = file_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                skipped_counts["unreadable"] = skipped_counts.get("unreadable", 0) + 1
                continue
            matches = _matched_terms(text, target.terms)
            if not matches:
                continue
            target_matches += 1
            matched_paths.append(relative_path.as_posix())
            if target_snippet_count >= MAX_SNIPPETS_PER_TARGET:
                continue
            target_snippet_count += 1
            snippet = _snippet_for_match(
                text=text,
                relative_path=relative_path,
                kind=kind,
                matched_terms=matches,
            )
            bucket = "test" if kind == "test" else "doc" if kind == "doc" else "source"
            snippet_buckets[bucket].setdefault(
                f"{kind}:{relative_path.as_posix()}",
                snippet,
            )
        search_observations.append(
            SearchObservation(
                query=target.authority_ref,
                match_count=target_matches,
                paths=matched_paths[:MAX_SNIPPETS_PER_TARGET],
            )
        )
    return (
        snippet_buckets["source"],
        snippet_buckets["test"],
        snippet_buckets["doc"],
        search_observations,
    )


def _targets_from_invariants(compiled: dict[str, Any]) -> list[AuthorityTarget]:
    invariants = compiled.get("invariants")
    if not isinstance(invariants, list):
        return []
    source_terms = _source_map_terms(compiled)
    targets: list[AuthorityTarget] = []
    for invariant in invariants:
        if not isinstance(invariant, dict):
            continue
        invariant_id = _str_or_none(invariant.get("id"))
        if not invariant_id:
            continue
        invariant_type = _str_or_none(invariant.get("type"))
        parameters = _dict_or_empty(invariant.get("parameters"))
        source_requirement_id = _str_or_none(parameters.get("source_item_id"))
        authority_ref = source_requirement_id or invariant_id
        terms = _unique_terms(
            [
                invariant_id,
                authority_ref,
                invariant_type,
                *source_terms.get(authority_ref, []),
                *_flatten_terms(parameters),
            ]
        )
        targets.append(
            AuthorityTarget(
                authority_ref=authority_ref,
                invariant_refs=[invariant_id],
                title=_title_from_ref(authority_ref),
                invariant_type=invariant_type,
                source_requirement_id=source_requirement_id,
                terms=terms,
                parameters=parameters,
            )
        )
    return targets


def _targets_from_items(compiled: dict[str, Any]) -> list[AuthorityTarget]:
    items = compiled.get("items")
    if not isinstance(items, list):
        return []
    targets: list[AuthorityTarget] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = _str_or_none(item.get("id"))
        if not item_id:
            continue
        item_type = _str_or_none(item.get("type"))
        targets.append(
            AuthorityTarget(
                authority_ref=item_id,
                invariant_refs=[],
                title=_title_from_ref(item_id),
                invariant_type=item_type,
                source_requirement_id=item_id,
                terms=_unique_terms([item_id, item_type, *_flatten_terms(item)]),
                parameters=item,
            )
        )
    return targets


def _source_map_terms(compiled: dict[str, Any]) -> dict[str, list[str]]:
    source_map = compiled.get("source_map")
    if not isinstance(source_map, list):
        return {}
    result: dict[str, list[str]] = {}
    for entry in source_map:
        if not isinstance(entry, dict):
            continue
        source_id = _str_or_none(entry.get("source_item_id"))
        if not source_id:
            continue
        result.setdefault(source_id, []).extend(_flatten_terms(entry))
    return result


def _flatten_terms(value: object) -> list[str]:
    if value is None or isinstance(value, bool | int | float):
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        terms: list[str] = []
        for item in value.values():
            terms.extend(_flatten_terms(item))
        return terms
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        terms = []
        for item in value:
            terms.extend(_flatten_terms(item))
        return terms
    return []


def _unique_terms(values: Iterable[str | None]) -> list[str]:
    seen: set[str] = set()
    terms: list[str] = []
    for value in values:
        if value is None:
            continue
        stripped = value.strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        terms.append(stripped)
    return terms


def _title_from_ref(ref: str) -> str:
    tail = ref.split(".", 1)[-1]
    return tail.replace("-", " ").replace("_", " ").title()


def _str_or_none(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _dict_or_empty(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _repo_snapshot(repo: Path) -> RepoSnapshot:
    git_commit: str | None = None
    dirty = False
    try:
        commit = subprocess.run(  # noqa: S603  # nosec B603
            [GIT_BINARY, "-C", str(repo), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
        if commit.returncode == 0:
            git_commit = commit.stdout.strip() or None
            status = subprocess.run(  # noqa: S603  # nosec B603
                [GIT_BINARY, "-C", str(repo), "status", "--porcelain"],
                check=False,
                capture_output=True,
                text=True,
            )
            dirty = bool(status.stdout.strip()) if status.returncode == 0 else False
    except OSError:
        git_commit = None
        dirty = False
    return RepoSnapshot(path=str(repo), git_commit=git_commit, dirty=dirty)


def _scannable_files(
    repo: Path,
) -> tuple[list[tuple[Path, Path, EvidenceKind]], dict[str, int]]:
    files: list[tuple[Path, Path, EvidenceKind]] = []
    skipped: dict[str, int] = {}
    for root, dir_names, file_names in repo.walk():
        skipped_dirs = [name for name in dir_names if name in _SKIP_DIR_NAMES]
        if skipped_dirs:
            skipped["runtime_dir"] = skipped.get("runtime_dir", 0) + len(skipped_dirs)
        dir_names[:] = [name for name in dir_names if name not in _SKIP_DIR_NAMES]
        for file_name in sorted(file_names):
            file_path = root / file_name
            relative_path = file_path.relative_to(repo)
            reason = _skip_file_reason(file_path, relative_path)
            if reason is not None:
                skipped[reason] = skipped.get(reason, 0) + 1
                continue
            files.append((file_path, relative_path, _file_kind(relative_path)))
    files.sort(key=lambda entry: entry[1].as_posix())
    return files[:MAX_FILE_MANIFEST_ENTRIES], skipped


def _skip_file_reason(file_path: Path, relative_path: Path) -> str | None:
    if not file_path.is_file():
        return "non_regular"
    if file_path.name in _SKIP_FILE_NAMES:
        return "lockfile"
    if file_path.suffix.lower() in _SKIP_SUFFIXES:
        return "unsupported_suffix"
    if file_path.stat().st_size > MAX_SCAN_BYTES:
        return "oversized"
    if any(part in _SKIP_DIR_NAMES for part in relative_path.parts):
        return "runtime_dir"
    return None


def _file_kind(relative_path: Path) -> EvidenceKind:
    parts = set(relative_path.parts[:-1])
    name = relative_path.name
    suffix = relative_path.suffix.lower()
    if "tests" in parts or "test" in parts:
        return "test"
    if name.startswith("test_") and suffix == ".py":
        return "test"
    if name.endswith("_test.py") or name.endswith(_TEST_SUFFIXES):
        return "test"
    if parts & _DOC_DIR_NAMES or suffix in _DOC_SUFFIXES:
        return "doc"
    if name in _CONFIG_NAMES or suffix in _CONFIG_SUFFIXES:
        return "config"
    return "source"


def _matched_terms(text: str, terms: list[str]) -> list[str]:
    return [term for term in terms if _term_matches(text, term)]


def _term_matches(text: str, term: str) -> bool:
    if _ID_TERM_RE.match(term):
        pattern = re.compile(rf"(?<![A-Za-z0-9_.-]){re.escape(term)}(?![A-Za-z0-9_.-])")
        return bool(pattern.search(text))
    return term.lower() in text.lower()


def _snippet_for_match(
    *,
    text: str,
    relative_path: Path,
    kind: EvidenceKind,
    matched_terms: list[str],
) -> EvidenceSnippet:
    lines = text.splitlines()
    first_match_line = _first_match_line(lines, matched_terms)
    start = max(first_match_line - 3, 1)
    end = min(start + MAX_SNIPPET_LINES - 1, len(lines) or 1)
    snippet_text = "\n".join(lines[start - 1 : end])
    encoded = snippet_text.encode("utf-8")
    if len(encoded) > MAX_SNIPPET_BYTES:
        snippet_text = encoded[:MAX_SNIPPET_BYTES].decode("utf-8", errors="ignore")
    return EvidenceSnippet(
        kind=kind,
        path=relative_path.as_posix(),
        line_start=start,
        line_end=end,
        matched_terms=matched_terms[:MAX_SNIPPETS_PER_TARGET],
        text=snippet_text,
        summary=f"Matched {len(matched_terms)} authority term(s).",
    )


def _first_match_line(lines: list[str], terms: list[str]) -> int:
    for index, line in enumerate(lines, start=1):
        if _matched_terms(line, terms):
            return index
    return 1


def _manifest_summary(
    *,
    files: list[tuple[Path, Path, EvidenceKind]],
    skipped_counts: dict[str, int],
    spec_file: Path | None,
    spec_mode: SpecMode,
    project_id: int,
) -> dict[str, Any]:
    kind_counts: dict[str, int] = {}
    for _path, _relative, kind in files:
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
    summary: dict[str, Any] = {
        "project_id": project_id,
        "spec_mode": spec_mode,
        "total_files": len(files) + sum(skipped_counts.values()),
        "included_files": len(files),
        "skipped_files": sum(skipped_counts.values()),
        "skipped_counts": skipped_counts,
        "kind_counts": kind_counts,
    }
    if spec_file is not None:
        try:
            summary["spec_file"] = {
                "path": str(spec_file),
                "sha256": _file_sha256(spec_file),
            }
        except OSError as exc:
            summary["spec_file"] = {
                "path": str(spec_file),
                "error": str(exc),
            }
    return summary


def _finalize_pack(pack: EvidencePack) -> EvidencePack:
    current = pack
    warnings = list(current.warnings)
    while len(canonical_json(_pack_fingerprint_payload(current))) > MAX_PACK_BYTES:
        if current.doc_snippets:
            current = current.model_copy(
                update={"doc_snippets": current.doc_snippets[:-1]}
            )
        elif current.test_snippets:
            current = current.model_copy(
                update={"test_snippets": current.test_snippets[:-1]}
            )
        elif current.source_snippets:
            current = current.model_copy(
                update={"source_snippets": current.source_snippets[:-1]}
            )
        else:
            break
        if not any(warning.code == "AS_BUILT_PACK_TRUNCATED" for warning in warnings):
            warnings.append(
                EvidenceWarning(
                    code="AS_BUILT_PACK_TRUNCATED",
                    message=(
                        "Evidence pack exceeded size cap and snippets were truncated."
                    ),
                    details={"max_pack_bytes": MAX_PACK_BYTES},
                )
            )
        current = current.model_copy(update={"warnings": warnings})
    fingerprint = canonical_hash(_pack_fingerprint_payload(current))
    return current.model_copy(update={"evidence_pack_fingerprint": fingerprint})


def _pack_fingerprint_payload(pack: EvidencePack) -> dict[str, Any]:
    payload = pack.model_dump(mode="json")
    payload.pop("evidence_pack_fingerprint", None)
    payload.pop("generated_at", None)
    return payload


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_assessment_json(assessment: AsBuiltAssessment) -> str:
    """Return canonical JSON for persisted assessment state."""
    return canonical_json(assessment.model_dump(mode="json"))


def assessment_from_json(value: str) -> AsBuiltAssessment:
    """Parse one persisted assessment JSON string."""
    return AsBuiltAssessment.model_validate_json(value)


def _json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return cast("dict[str, Any]", decoded) if isinstance(decoded, dict) else {}
