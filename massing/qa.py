"""Pure QA aggregation and deterministic Markdown rendering for massing builds.

No I/O, no wall clock and no output paths: tool version, runtime, dependency versions,
config hashes and source manifests are supplied explicitly by the caller, so fixed inputs
give byte-identical Markdown. Nothing is corrected here; QA only counts and lists.

Reconciliation: input footprints = output features + geometry exclusions + extrusion
failures, and output ids must equal prepared ids minus extrusion failures and the ids of
the height decisions.
Width measure: shorter side of the minimum rotated rectangle of the prepared EPSG:26910
footprint (``simplify.footprint_width_m``). Buildings with height_m / width_m above
HEIGHT_WIDTH_RATIO_THRESHOLD (8.0, a declared review threshold, not a physical limit) or a
zero width are flagged for review. Storey counts above 60 are flagged, never clamped.
Confidence is a declared evidence score, not a statistical probability.
Footprint-geometry licences and height-evidence licences are reported separately.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from massing.extrude import Mesh
from massing.heights import (
    CONFIDENCE,
    LADDER,
    STOREY_OUTLIER_THRESHOLD,
    EvidenceIssue,
    HeightDecision,
    HeightSource,
)
from massing.match import MatchResult
from massing.osm import OsmJoinResult
from massing.simplify import PreparedBatch

HEIGHT_WIDTH_RATIO_THRESHOLD = 8.0
SOURCE_ROLES = ("footprints", "height_evidence", "applications", "overrides")
RUN_MODES = frozenset({"offline", "live"})
_FEET = re.compile(r"\b(?:ft|foot|feet)\b", re.IGNORECASE)
_ROLE_TITLES = {
    "footprints": "Footprint geometry sources (licence covers geometry only)",
    "height_evidence": "Height evidence sources (licence covers height evidence only)",
    "applications": "Development application sources",
    "overrides": "Reviewed override files",
}


def _line(value: str, what: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value:
        raise ValueError(f"{what} must be a non-empty single-line string")
    return value


@dataclass(frozen=True, slots=True)
class SourceManifest:
    role: str
    name: str
    url: str
    licence: str
    retrieved_at: str | None = None
    sha256: str | None = None
    record_count: int | None = None
    total_available: int | None = None
    truncated: bool | None = None
    test_only: bool = False
    licence_url: str | None = None
    query: str | None = None

    def __post_init__(self) -> None:
        if self.role not in SOURCE_ROLES:
            raise ValueError(f"source role {self.role!r} must be one of {SOURCE_ROLES}")
        for what in ("name", "url", "licence"):
            _line(getattr(self, what), f"source {what}")


@dataclass(frozen=True, slots=True)
class RunInfo:
    tool_version: str
    python_version: str
    mode: str
    command: str = "build"
    dependency_versions: tuple[tuple[str, str], ...] = ()
    config_hashes: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in RUN_MODES:
            raise ValueError(f"mode must be one of {sorted(RUN_MODES)}")
        for what in ("tool_version", "python_version", "command"):
            _line(getattr(self, what), what)


@dataclass(frozen=True, slots=True)
class ExtrusionFailure:
    building_id: str
    detail: str


@dataclass(frozen=True, slots=True)
class StoreyCandidate:
    """Application storey evidence offered for a footprint (or unmatched, footprint None)."""

    site_id: str
    footprint_id: str | None
    storeys: int | None
    status: str
    reference: str | None
    used: bool


@dataclass(frozen=True, slots=True)
class QAInputs:
    run: RunInfo
    sources: tuple[SourceManifest, ...]
    input_footprint_count: int
    prepared: PreparedBatch
    decisions: Mapping[str, HeightDecision]
    output_ids: tuple[str, ...]
    extrusion_failures: tuple[ExtrusionFailure, ...] = ()
    meshes: tuple[Mesh, ...] = ()
    match: MatchResult | None = None
    osm: OsmJoinResult | None = None
    application_candidates: tuple[StoreyCandidate, ...] = ()
    override_rows_total: int = 0
    override_issues: tuple[EvidenceIssue, ...] = ()
    ratio_threshold: float = HEIGHT_WIDTH_RATIO_THRESHOLD


@dataclass(frozen=True, slots=True)
class QAFinding:
    category: str
    subject_id: str
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class Reconciliation:
    input_count: int
    prepared_count: int
    geometry_excluded: int
    extrusion_failed: int
    output_count: int
    reconciled: bool
    discrepancies: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StoreyRow:
    building_id: str
    current: int | None
    current_source: str
    alternatives: tuple[int, ...]
    history: tuple[int, ...]
    candidates: tuple[StoreyCandidate, ...]


@dataclass(frozen=True, slots=True)
class StoreyOutlier:
    subject_id: str
    storeys: int
    origin: str


@dataclass(frozen=True, slots=True)
class RatioOutlier:
    building_id: str
    height_m: float
    width_m: float
    ratio: float


@dataclass(frozen=True, slots=True)
class QAReport:
    run: RunInfo
    sources: tuple[SourceManifest, ...]
    reconciliation: Reconciliation
    source_counts: tuple[tuple[str, int], ...]
    real_override_ids: tuple[str, ...]
    test_only_override_ids: tuple[str, ...]
    unknown_ids: tuple[str, ...]
    typology_ids: tuple[str, ...]
    evidence_issues: tuple[EvidenceIssue, ...]
    override_rows_total: int
    override_issues: tuple[EvidenceIssue, ...]
    findings: tuple[QAFinding, ...]
    finding_counts: tuple[tuple[str, str, int], ...]
    unmatched_site_ids: tuple[str, ...]
    unmatched_footprint_ids: tuple[str, ...]
    conflicted_footprint_ids: tuple[str, ...]
    osm_summary: tuple[tuple[str, int], ...]
    storey_rows: tuple[StoreyRow, ...]
    storey_outliers: tuple[StoreyOutlier, ...]
    ratio_threshold: float
    ratio_outliers: tuple[RatioOutlier, ...]
    feet_ids: tuple[str, ...]

    def to_markdown(self) -> str:
        return render_markdown(self)


def _issue_key(issue: EvidenceIssue) -> tuple[str, str, str, int, str]:
    return (issue.building_id, issue.source, issue.code,
            -1 if issue.row is None else issue.row, issue.message)


def _validate(inputs: QAInputs) -> None:
    count = inputs.input_footprint_count
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("input_footprint_count must be a nonnegative int")
    rows = inputs.override_rows_total
    if isinstance(rows, bool) or not isinstance(rows, int) or rows < 0:
        raise ValueError("override_rows_total must be a nonnegative int")
    threshold = inputs.ratio_threshold
    if isinstance(threshold, bool) or not math.isfinite(threshold) or threshold <= 0:
        raise ValueError("ratio_threshold must be finite and > 0")


def _reconcile(inputs: QAInputs) -> Reconciliation:
    prepared = inputs.prepared
    prepared_ids = {f.building_id for f in prepared.footprints}
    failed = {f.building_id for f in inputs.extrusion_failures}
    output = list(inputs.output_ids)
    problems: list[str] = []
    if prepared.input_count != inputs.input_footprint_count:
        problems.append(f"prepared input_count {prepared.input_count} != "
                        f"input footprints {inputs.input_footprint_count}")
    if not prepared.reconciled:
        problems.append("geometry stage: prepared + excluded != prepared input_count")
    if len(set(output)) != len(output):
        problems.append("duplicate output ids")
    if failed - prepared_ids:
        problems.append(f"extrusion failures for unknown ids {sorted(failed - prepared_ids)}")
    expected = prepared_ids - failed
    if set(output) != expected:
        problems.append(f"output ids missing {sorted(expected - set(output))}, "
                        f"unexpected {sorted(set(output) - expected)}")
    if set(inputs.decisions) != set(output):
        problems.append("height decisions do not match output ids")
    excluded = len(prepared.exclusions) + len(failed)
    if inputs.input_footprint_count != len(set(output)) + excluded:
        problems.append(f"input {inputs.input_footprint_count} != output {len(set(output))} "
                        f"+ excluded {excluded}")
    return Reconciliation(inputs.input_footprint_count, len(prepared_ids),
                          len(prepared.exclusions), len(failed), len(set(output)),
                          not problems, tuple(problems))


def _findings(inputs: QAInputs) -> list[QAFinding]:
    out: list[QAFinding] = []
    out.extend(QAFinding("geometry", c.building_id, c.code, c.detail)
               for c in inputs.prepared.changes)
    out.extend(QAFinding("geometry_exclusion", e.building_id, e.code, e.detail)
               for e in inputs.prepared.exclusions)
    out.extend(QAFinding("extrusion", f.building_id, "extrusion_failed", f.detail)
               for f in inputs.extrusion_failures)
    for mesh in inputs.meshes:
        if mesh.cap_triangles != mesh.expected_cap_triangles:
            out.append(QAFinding("extrusion", mesh.building_id, "cap_triangle_count",
                                 f"{mesh.cap_triangles} per cap vs expected "
                                 f"{mesh.expected_cap_triangles} (collinear vertices)"))
    if inputs.match is not None:
        out.extend(QAFinding("matching", f.subject_id, f.code,
                             f"{f.subject}: {f.detail}"
                             + (f"; candidates {list(f.candidate_ids)}" if f.candidate_ids else ""))
                   for f in inputs.match.findings)
    if inputs.osm is not None:
        out.extend(QAFinding("osm", f.subject_id, f.code,
                             f"{f.subject}: {f.detail}"
                             + (f"; candidates {list(f.candidate_ids)}" if f.candidate_ids else ""))
                   for f in inputs.osm.findings)
    return sorted(out, key=lambda f: (f.category, f.subject_id, f.code, f.detail))


def build_qa(inputs: QAInputs) -> QAReport:
    _validate(inputs)
    decisions = [inputs.decisions[i] for i in sorted(inputs.decisions)]
    city = HeightSource.CITY_SUPPLIED
    findings = _findings(inputs)
    counts = Counter((f.category, f.code) for f in findings)
    candidates_by_fp: dict[str, list[StoreyCandidate]] = {}
    for cand in inputs.application_candidates:
        if cand.footprint_id is not None:
            candidates_by_fp.setdefault(cand.footprint_id, []).append(cand)
    storey_rows: list[StoreyRow] = []
    outliers: list[StoreyOutlier] = []
    for d in decisions:
        alternatives = tuple(s.storeys for s in d.alternatives if s.storeys is not None)
        history = tuple(s.storeys for s in d.history if s.storeys is not None)
        cands = tuple(sorted(candidates_by_fp.get(d.building_id, ()),
                             key=lambda c: (c.site_id, c.storeys or 0)))
        if d.storeys is not None or alternatives or history or cands:
            storey_rows.append(StoreyRow(d.building_id, d.storeys, d.source.value,
                                         alternatives, history, cands))
        for origin, values in (("current", (d.storeys,)), ("alternative", alternatives),
                               ("history", history)):
            outliers.extend(StoreyOutlier(d.building_id, v, origin) for v in values
                            if v is not None and v > STOREY_OUTLIER_THRESHOLD)
    outliers.extend(StoreyOutlier(f"site:{c.site_id}", c.storeys, "application_candidate")
                    for c in inputs.application_candidates
                    if c.storeys is not None and c.storeys > STOREY_OUTLIER_THRESHOLD)
    widths = {f.building_id: f.width_m for f in inputs.prepared.footprints}
    ratios: list[RatioOutlier] = []
    for d in decisions:
        width = widths.get(d.building_id)
        if width is None:
            continue
        ratio = d.height_m / width if width > 0 else math.inf
        if ratio > inputs.ratio_threshold:
            ratios.append(RatioOutlier(d.building_id, d.height_m, width, ratio))
    osm_summary: tuple[tuple[str, int], ...] = ()
    if inputs.osm is not None:
        osm_summary = (("elements", inputs.osm.element_count),
                       ("usable_closed_ways", inputs.osm.usable_way_count),
                       ("accepted_matches", len(inputs.osm.matches)),
                       ("footprints_without_osm", len(inputs.osm.unmatched_footprint_ids)),
                       ("findings", len(inputs.osm.findings)))
    match = inputs.match
    return QAReport(
        run=inputs.run,
        sources=tuple(sorted(inputs.sources, key=lambda s: (SOURCE_ROLES.index(s.role),
                                                            s.name, s.url))),
        reconciliation=_reconcile(inputs),
        source_counts=tuple((s.value, sum(1 for d in decisions if d.source is s))
                            for s in LADDER),
        real_override_ids=tuple(d.building_id for d in decisions
                                if d.source is city and not d.test_only),
        test_only_override_ids=tuple(d.building_id for d in decisions if d.test_only),
        unknown_ids=tuple(d.building_id for d in decisions
                          if d.source is HeightSource.UNKNOWN),
        typology_ids=tuple(d.building_id for d in decisions
                           if d.source is HeightSource.TYPOLOGY_DEFAULT),
        evidence_issues=tuple(sorted((i for d in decisions for i in d.issues), key=_issue_key)),
        override_rows_total=inputs.override_rows_total,
        override_issues=tuple(sorted(inputs.override_issues, key=_issue_key)),
        findings=tuple(findings),
        finding_counts=tuple((cat, code, n) for (cat, code), n in sorted(counts.items())),
        unmatched_site_ids=match.unmatched_site_ids if match else (),
        unmatched_footprint_ids=match.unmatched_footprint_ids if match else (),
        conflicted_footprint_ids=match.conflicted_footprint_ids if match else (),
        osm_summary=osm_summary,
        storey_rows=tuple(storey_rows),
        storey_outliers=tuple(sorted(outliers, key=lambda o: (o.subject_id, o.origin,
                                                              o.storeys))),
        ratio_threshold=inputs.ratio_threshold,
        ratio_outliers=tuple(ratios),
        feet_ids=tuple(d.building_id for d in decisions
                       if d.source_text is not None and _FEET.search(d.source_text)),
    )


def _cell(value: object) -> str:
    if value is None:
        text = ""
    elif isinstance(value, bool):
        text = "yes" if value else "no"
    elif isinstance(value, float):
        text = "inf" if math.isinf(value) else format(value, ".3f")
    else:
        text = str(value)
    return (text.replace("\\", "\\\\").replace("|", "\\|")
            .replace("\r", " ").replace("\n", " "))


def _table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> list[str]:
    if not rows:
        return ["_None._", ""]
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines.extend("| " + " | ".join(_cell(c) for c in row) + " |" for row in rows)
    lines.append("")
    return lines


def _ids(values: Sequence[str]) -> list[str]:
    return [", ".join(_cell(v) for v in values) if values else "_None._", ""]


def _pct(n: int, total: int) -> str:
    return f"{100.0 * n / total:.1f}%" if total else "n/a"


def render_markdown(report: QAReport) -> str:
    r = report.reconciliation
    run = report.run
    total = r.output_count
    lines = ["# Massing QA report", ""]
    if not r.reconciled:
        lines += ["**ATTENTION: counts do not reconcile; see Reconciliation.**", ""]
    if report.test_only_override_ids:
        lines += ["**ATTENTION: test-only overrides present; they are NOT City evidence.**", ""]
    lines += ["## Run", "",
              f"- Tool version: {_cell(run.tool_version)}",
              f"- Python runtime: {_cell(run.python_version)}",
              f"- Command: {_cell(run.command)}; mode: {run.mode}",
              "- Reproducibility: offline builds are reproducible for fixed inputs; "
              "live public data may change and is outside that guarantee.", ""]
    lines += _table(("Dependency", "Version"), sorted(run.dependency_versions))
    lines += _table(("Config origin", "SHA-256"), sorted(run.config_hashes))
    lines += ["## Sources and licences", ""]
    for role in SOURCE_ROLES:
        rows = [(s.name, s.url, s.licence, s.licence_url, s.retrieved_at, s.sha256,
                 s.record_count, s.total_available, s.truncated, s.test_only, s.query)
                for s in report.sources if s.role == role]
        lines += [f"### {_ROLE_TITLES[role]}", ""]
        lines += _table(("Name", "URL", "Licence", "Licence URL", "Retrieved", "SHA-256",
                         "Records", "Available", "Truncated", "Test-only", "Query"), rows)
    lines += ["## Reconciliation", ""]
    lines += _table(("Measure", "Count"), [
        ("Input footprints", r.input_count), ("Prepared footprints", r.prepared_count),
        ("Geometry exclusions", r.geometry_excluded), ("Extrusion failures", r.extrusion_failed),
        ("Output features", r.output_count), ("Reconciled", r.reconciled)])
    lines += [f"- {_cell(p)}" for p in r.discrepancies] + ([""] if r.discrepancies else [])
    lines += ["## Height evidence", "",
              "Confidence is a declared evidence score, not a statistical probability.", ""]
    lines += _table(("Source", "Confidence", "Buildings", "Share"),
                    [(s, CONFIDENCE[HeightSource(s)], n, _pct(n, total))
                     for s, n in report.source_counts])
    weak = len(report.unknown_ids) + len(report.typology_ids)
    lines += [f"- Uncertain (typology_default + unknown, dashed outline): {weak} of {total} "
              f"({_pct(weak, total)})",
              f"- Reviewed real City overrides: {len(report.real_override_ids)}",
              f"- Test-only overrides (not City evidence): {len(report.test_only_override_ids)}",
              ""]
    lines += ["### Unknown (4 m placeholder, confidence 0.1)", "", *_ids(report.unknown_ids)]
    lines += ["### Typology defaults (confidence 0.3)", "", *_ids(report.typology_ids)]
    lines += ["### Evidence issues", ""]
    lines += _table(("Building", "Source", "Code", "Raw", "Message"),
                    [(i.building_id, i.source, i.code, i.raw_value, i.message)
                     for i in report.evidence_issues])
    lines += ["## Override rows", "",
              f"- Rows read: {report.override_rows_total}",
              f"- Applied real overrides: {len(report.real_override_ids)}",
              f"- Applied test-only overrides: {len(report.test_only_override_ids)}",
              f"- Rejected or unmatched: {len(report.override_issues)}", ""]
    lines += _table(("Id", "Row", "Code", "Message"),
                    [(i.building_id, i.row, i.code, i.message) for i in report.override_issues])
    lines += ["## Findings by category", ""]
    lines += _table(("Category", "Code", "Count"), report.finding_counts)
    lines += _table(("Category", "Subject", "Code", "Detail"),
                    [(f.category, f.subject_id, f.code, f.detail) for f in report.findings])
    lines += ["## Matching", ""]
    lines += ["### Unmatched sites", "", *_ids(report.unmatched_site_ids)]
    lines += ["### Footprints without a site match", "", *_ids(report.unmatched_footprint_ids)]
    lines += ["### Footprints with conflicting projects", "",
              *_ids(report.conflicted_footprint_ids)]
    lines += ["## OpenStreetMap join", ""]
    lines += _table(("Measure", "Count"), report.osm_summary)
    lines += ["## Storeys", ""]
    lines += _table(("Building", "Current", "Current source", "Alternatives", "History",
                     "Application candidates"),
                    [(s.building_id, s.current, s.current_source,
                      ", ".join(str(v) for v in s.alternatives),
                      ", ".join(str(v) for v in s.history),
                      "; ".join(f"{c.site_id}={c.storeys if c.storeys is not None else '-'}"
                                f" ({c.status}{', used' if c.used else ''})"
                                for c in s.candidates))
                     for s in report.storey_rows])
    lines += [f"### Storey counts above {STOREY_OUTLIER_THRESHOLD} (not corrected)", ""]
    lines += _table(("Subject", "Storeys", "Origin"),
                    [(o.subject_id, o.storeys, o.origin) for o in report.storey_outliers])
    lines += [f"## Height-to-width outliers (ratio > {report.ratio_threshold:.1f})", "",
              "Width = shorter side of the minimum rotated rectangle (EPSG:26910 metres). "
              "Flagged for review only; heights are not corrected.", ""]
    lines += _table(("Building", "Height m", "Width m", "Ratio"),
                    [(o.building_id, o.height_m, o.width_m, o.ratio)
                     for o in report.ratio_outliers])
    lines += ["## Feet conversions in winning evidence", "",
              "Detected from winning evidence source text; override-CSV units are not "
              "retained after parsing and are not listed here.", ""]
    lines += _ids(report.feet_ids)
    return "\n".join(lines).rstrip("\n") + "\n"
