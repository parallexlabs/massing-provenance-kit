"""Join application sites to prepared footprints in EPSG:26910 (pure).

Ambiguity guard for every site type: no candidates, or more than three candidates,
produce a finding and no match; nothing is guessed.
Polygon/MultiPolygon sites: candidates are intersecting footprints. With 1..3
candidates all are matched (method "polygon_intersects"), intersection area recorded;
boundary-only contact (area 0) is matched but also reported.
Point sites: candidates are footprints within 25 m (distance to the footprint polygon,
0 inside) ranked by distance then id. With 1..3 candidates the nearest is matched
(method "point_nearest_within_25m") unless the nearest two differ by <= 0.01 m (tie).
A footprint matched by sites with different project references is a conflict.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import shapely

from massing.heights import EvidenceError, validate_building_id
from massing.simplify import GeometryError, check_wgs84, parse_geometry, repair, to_projected

POINT_RADIUS_M = 25.0
MAX_CANDIDATES = 3
MAX_POINT_CANDIDATES = MAX_CANDIDATES
TIE_TOLERANCE_M = 0.01
METHOD_POLYGON = "polygon_intersects"
METHOD_POINT = "point_nearest_within_25m"


@dataclass(frozen=True, slots=True)
class Site:
    site_id: str
    geometry: object
    project_ref: str | None = None


@dataclass(frozen=True, slots=True)
class SiteMatch:
    site_id: str
    footprint_id: str
    method: str
    candidate_count: int
    project_ref: str | None
    distance_m: float | None = None
    intersection_area_m2: float | None = None


@dataclass(frozen=True, slots=True)
class MatchFinding:
    subject: str
    subject_id: str
    code: str
    detail: str
    candidate_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MatchResult:
    site_count: int
    footprint_count: int
    matches: tuple[SiteMatch, ...]
    findings: tuple[MatchFinding, ...]
    unmatched_site_ids: tuple[str, ...]
    unmatched_footprint_ids: tuple[str, ...]
    conflicted_footprint_ids: tuple[str, ...]


def _site_geometry(site: Site) -> tuple[Any, list[MatchFinding]]:
    geom = parse_geometry(site.geometry)
    if geom.is_empty:
        raise GeometryError("empty_geometry", "site geometry is empty")
    if geom.geom_type not in ("Point", "Polygon", "MultiPolygon"):
        raise GeometryError("unsupported_site_geometry", f"{geom.geom_type} site not supported")
    if shapely.has_z(geom):
        geom = shapely.force_2d(geom)
    check_wgs84(geom)
    utm = to_projected(geom)
    findings: list[MatchFinding] = []
    if utm.geom_type != "Point" and not shapely.is_valid(utm):
        reason = str(shapely.is_valid_reason(utm))
        utm, dropped = repair(utm)
        findings.append(MatchFinding("site", site.site_id, "site_repaired",
                                     f"{reason}; dropped parts {dropped}"))
    return utm, findings


def _too_many(sid: str, cands: tuple[str, ...], what: str) -> MatchFinding:
    return MatchFinding("site", sid, "too_many_candidates",
                        f"{len(cands)} candidates ({what}) > {MAX_CANDIDATES}; not matched",
                        cands)


def _match_point(sid: str, site: Site, geom: Any, tree: Any, ids: list[str], geoms: list[Any]
                 ) -> tuple[list[SiteMatch], list[MatchFinding]]:
    idx = tree.query(geom, predicate="dwithin", distance=POINT_RADIUS_M)
    ranked = sorted((float(geom.distance(geoms[int(i)])), ids[int(i)]) for i in idx)
    cands = tuple(fid for _, fid in ranked)
    if not ranked:
        return [], [MatchFinding("site", sid, "no_candidates",
                                 f"no footprint within {POINT_RADIUS_M} m")]
    if len(ranked) > MAX_CANDIDATES:
        return [], [_too_many(sid, cands, f"within {POINT_RADIUS_M} m")]
    if len(ranked) > 1 and ranked[1][0] - ranked[0][0] <= TIE_TOLERANCE_M:
        return [], [MatchFinding("site", sid, "distance_tie",
                                 f"nearest distances {ranked[0][0]:.3f} and "
                                 f"{ranked[1][0]:.3f} m", cands)]
    return [SiteMatch(sid, ranked[0][1], METHOD_POINT, len(ranked), site.project_ref,
                      distance_m=round(ranked[0][0], 3))], []


def _match_polygon(sid: str, site: Site, geom: Any, tree: Any, ids: list[str],
                   footprints: Mapping[str, Any]
                   ) -> tuple[list[SiteMatch], list[MatchFinding]]:
    idx = tree.query(geom, predicate="intersects")
    cands = tuple(sorted(ids[int(i)] for i in idx))
    if not cands:
        return [], [MatchFinding("site", sid, "no_candidates",
                                 "site polygon intersects no footprint")]
    areas = {fid: round(float(geom.intersection(footprints[fid]).area), 3) for fid in cands}
    if len(cands) > MAX_CANDIDATES:
        listing = ", ".join(f"{fid}={areas[fid]:.3f} m2" for fid in cands)
        return [], [_too_many(sid, cands, f"intersecting; {listing}")]
    matches: list[SiteMatch] = []
    findings: list[MatchFinding] = []
    for fid in cands:
        if areas[fid] == 0.0:
            findings.append(MatchFinding("site", sid, "boundary_touch_only",
                                         f"site only touches footprint {fid}", (fid,)))
        matches.append(SiteMatch(sid, fid, METHOD_POLYGON, len(cands), site.project_ref,
                                 intersection_area_m2=areas[fid]))
    return matches, findings


def match_sites(sites: Sequence[Site], footprints: Mapping[str, Any]) -> MatchResult:
    """footprints: stable id -> EPSG:26910 Polygon/MultiPolygon (PreparedFootprint.geom_utm)."""
    ids = sorted(footprints)
    geoms = [footprints[i] for i in ids]
    tree = shapely.STRtree(geoms)
    matches: list[SiteMatch] = []
    findings: list[MatchFinding] = []
    site_counts = Counter(s.site_id for s in sites)
    for site in sites:
        sid = site.site_id
        try:
            validate_building_id(sid)
        except EvidenceError as exc:
            findings.append(MatchFinding("site", repr(sid)[:130], exc.code, str(exc)))
            continue
        if site_counts[sid] > 1:
            findings.append(MatchFinding("site", sid, "duplicate_site_id",
                                         f"site id occurs {site_counts[sid]} times; not matched"))
            continue
        try:
            geom, repairs = _site_geometry(site)
        except GeometryError as exc:
            findings.append(MatchFinding("site", sid, exc.code, str(exc)))
            continue
        findings.extend(repairs)
        if geom.geom_type == "Point":
            new_matches, new_findings = _match_point(sid, site, geom, tree, ids, geoms)
        else:
            new_matches, new_findings = _match_polygon(sid, site, geom, tree, ids, footprints)
        matches.extend(new_matches)
        findings.extend(new_findings)
    projects: dict[str, set[str]] = defaultdict(set)
    sites_by_fp: dict[str, set[str]] = defaultdict(set)
    for m in matches:
        projects[m.footprint_id].add(m.project_ref if m.project_ref else f"site:{m.site_id}")
        sites_by_fp[m.footprint_id].add(m.site_id)
    conflicted = tuple(sorted(fid for fid, refs in projects.items() if len(refs) > 1))
    for fid in conflicted:
        findings.append(MatchFinding("footprint", fid, "conflicting_project_matches",
                                     f"projects {sorted(projects[fid])}",
                                     tuple(sorted(sites_by_fp[fid]))))
    matched_sites = {m.site_id for m in matches}
    return MatchResult(
        site_count=len(sites),
        footprint_count=len(ids),
        matches=tuple(sorted(matches, key=lambda m: (m.site_id, m.footprint_id))),
        findings=tuple(findings),
        unmatched_site_ids=tuple(sorted({s.site_id for s in sites} - matched_sites)),
        unmatched_footprint_ids=tuple(fid for fid in ids if fid not in projects),
        conflicted_footprint_ids=conflicted,
    )
