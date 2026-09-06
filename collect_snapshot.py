#!/usr/bin/env python3
"""Сбор сырых нормализованных snapshot'ов АЗС вдоль маршрута Екб→Нск.

Только данные. Без отчётов, трендов и человеческих выводов.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE = "https://gdebenz.ru"
OSRM = "https://router.project-osrm.org/route/v1/driving"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

WAYPOINTS: list[tuple[str, float, float]] = [
    ("Екатеринбург", 56.8389, 60.6057),
    ("Тюмень", 57.1522, 65.5272),
    ("Ишим", 56.1120, 69.4903),
    ("Омск", 54.9885, 73.3242),
    ("Барабинск", 55.3460, 78.3460),
    ("Новосибирск", 55.0084, 82.9357),
]

RETRY_SLEEP_S = (30, 90)


@dataclass
class PolyPoint:
    lat: float
    lon: float
    s_km: float


class TransientError(RuntimeError):
    """Временная сетевая/серверная ошибка — можно ретраить."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_api_time(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(s.replace("Z", ""), fmt.replace("Z", ""))
            return to_iso_z(dt.replace(tzinfo=timezone.utc))
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return to_iso_z(dt)
    except ValueError:
        return None


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, TransientError):
        return True
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (429, 500, 502, 503, 504)
    if isinstance(exc, urllib.error.URLError):
        return True
    msg = str(exc).lower()
    return any(x in msg for x in ("timed out", "timeout", "dns", "temporary", "connection reset"))


def http_get_json(url: str, headers: dict[str, str] | None = None, timeout: float = 30.0) -> Any:
    req = urllib.request.Request(
        url,
        headers=headers
        or {
            "User-Agent": UA,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (429, 500, 502, 503, 504):
            raise TransientError(f"HTTP {exc.code} for {url}") from exc
        raise RuntimeError(f"HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise TransientError(f"network error for {url}: {exc}") from exc


def with_retries(fn, *, label: str):
    last: BaseException | None = None
    for attempt in range(3):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if not is_transient(exc) or attempt == 2:
                break
            sleep_s = RETRY_SLEEP_S[min(attempt, len(RETRY_SLEEP_S) - 1)]
            print(
                f"retry {attempt + 1}/3 after {sleep_s}s ({label}): {exc}",
                file=sys.stderr,
            )
            time.sleep(sleep_s)
    assert last is not None
    raise last


class GdeBenzClient:
    def __init__(self, pause_s: float = 0.2) -> None:
        self.pause_s = pause_s
        self._rt = ""
        self._rt_at = 0.0

    def _headers(self, with_rt: bool = True) -> dict[str, str]:
        h = {
            "User-Agent": UA,
            "Accept": "application/json",
            "Referer": f"{BASE}/",
            "Origin": BASE,
        }
        if with_rt:
            rt = self.get_rt()
            if rt:
                h["X-RT"] = rt
        return h

    def get_rt(self) -> str:
        if self._rt and (time.time() - self._rt_at) < 1500:
            return self._rt

        def _do() -> str:
            data = http_get_json(f"{BASE}/api/rt", headers=self._headers(with_rt=False))
            return str(data.get("rt") or "")

        self._rt = with_retries(_do, label="/api/rt")
        self._rt_at = time.time()
        return self._rt

    def _get(self, url: str, with_rt: bool = True) -> Any:
        def _do() -> Any:
            return http_get_json(url, headers=self._headers(with_rt=with_rt))

        data = with_retries(_do, label=url.split("?")[0])
        if self.pause_s > 0:
            time.sleep(self.pause_s)
        return data

    def stations(self, lat1: float, lon1: float, lat2: float, lon2: float) -> list[dict]:
        q = urllib.parse.urlencode(
            {
                "lat1": f"{lat1:.4f}",
                "lon1": f"{lon1:.4f}",
                "lat2": f"{lat2:.4f}",
                "lon2": f"{lon2:.4f}",
            }
        )
        data = self._get(f"{BASE}/api/stations?{q}")
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("stations"), list):
            return data["stations"]
        return []

    def comments_recent(self, osm_id: str, limit: int = 5) -> list[dict]:
        data = self._get(f"{BASE}/api/comments/{urllib.parse.quote(str(osm_id))}/recent?limit={limit}")
        return data if isinstance(data, list) else []


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def deg_box(lat: float, lon: float, half_km: float) -> tuple[float, float, float, float]:
    dlat = half_km / 111.0
    dlon = half_km / (111.0 * max(0.2, math.cos(math.radians(lat))))
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon


def point_to_segment_km(
    lat: float, lon: float, a_lat: float, a_lon: float, b_lat: float, b_lon: float
) -> tuple[float, float]:
    lat_m = 111_320.0
    lon_m = 111_320.0 * math.cos(math.radians(lat))
    ax = (a_lon - lon) * lon_m
    ay = (a_lat - lat) * lat_m
    bx = (b_lon - lon) * lon_m
    by = (b_lat - lat) * lat_m
    abx, aby = bx - ax, by - ay
    denom = abx * abx + aby * aby
    if denom <= 1e-6:
        return haversine_km(lat, lon, a_lat, a_lon), 0.0
    t = max(0.0, min(1.0, (-ax * abx - ay * aby) / denom))
    cx = ax + t * abx
    cy = ay + t * aby
    return math.hypot(cx, cy) / 1000.0, t


def build_polyline(coords_lonlat: list[list[float]]) -> list[PolyPoint]:
    pts: list[PolyPoint] = []
    s = 0.0
    prev: tuple[float, float] | None = None
    for lon, lat in coords_lonlat:
        if prev is not None:
            s += haversine_km(prev[0], prev[1], lat, lon)
        pts.append(PolyPoint(lat=lat, lon=lon, s_km=s))
        prev = (lat, lon)
    return pts


def dist_and_s_on_polyline(lat: float, lon: float, poly: list[PolyPoint]) -> tuple[float, float]:
    best_d = 1e18
    best_s = 0.0
    for i in range(len(poly) - 1):
        a, b = poly[i], poly[i + 1]
        d, t = point_to_segment_km(lat, lon, a.lat, a.lon, b.lat, b.lon)
        if d < best_d:
            best_d = d
            best_s = a.s_km + t * (b.s_km - a.s_km)
    return best_d, best_s


def sample_polyline(poly: list[PolyPoint], step_km: float) -> list[tuple[float, float]]:
    if not poly:
        return []
    out: list[tuple[float, float]] = [(poly[0].lat, poly[0].lon)]
    target = step_km
    total = poly[-1].s_km
    i = 1
    while target < total and i < len(poly):
        while i < len(poly) and poly[i].s_km < target:
            i += 1
        if i >= len(poly):
            break
        a, b = poly[i - 1], poly[i]
        span = max(1e-9, b.s_km - a.s_km)
        t = (target - a.s_km) / span
        out.append((a.lat + (b.lat - a.lat) * t, a.lon + (b.lon - a.lon) * t))
        target += step_km
    end = (poly[-1].lat, poly[-1].lon)
    if haversine_km(out[-1][0], out[-1][1], end[0], end[1]) > 1.0:
        out.append(end)
    return out


def fetch_osrm_route() -> tuple[list[PolyPoint], dict[str, Any]]:
    coords = ";".join(f"{lon},{lat}" for _, lat, lon in WAYPOINTS)
    url = f"{OSRM}/{coords}?overview=full&geometries=geojson&steps=false"

    def _do() -> Any:
        return http_get_json(url, timeout=90)

    data = with_retries(_do, label="OSRM")
    if data.get("code") != "Ok" or not data.get("routes"):
        raise RuntimeError(f"OSRM failed: {data.get('code')}")
    route = data["routes"][0]
    poly = build_polyline(route["geometry"]["coordinates"])
    meta = {
        "provider": "router.project-osrm.org",
        "distance_km": round(route["distance"] / 1000.0, 1),
        "duration_h": round(route["duration"] / 3600.0, 2),
        "points": len(poly),
    }
    return poly, meta


def is_target_brand(brand: str | None, name: str | None) -> bool:
    s = f"{brand or ''} {name or ''}".lower().replace("ё", "е")
    return ("лукойл" in s or "lukoil" in s) or ("газпром" in s or "gazprom" in s)


def network_norm(brand: str | None, name: str | None) -> str:
    low = f"{brand or ''} {name or ''}".lower().replace("ё", "е")
    if "газпромнефть" in low or "gazpromneft" in low or "газпром нефть" in low:
        return "Газпромнефть"
    if "лукойл" in low or "lukoil" in low:
        return "Лукойл"
    if "газпром" in low or "gazprom" in low:
        return "Газпром"
    return "unknown"


def parse_grades(text: str | None) -> set[str]:
    if not text:
        return set()
    t = str(text).upper().replace("АИ-", "").replace("AI-", "")
    found: set[str] = set()
    for g in ("100", "98", "95", "92"):
        if re.search(rf"(?<!\d){g}(?!\d)", t):
            found.add(g)
    if "ДТ" in t or "DT" in t or "ДИЗЕЛЬ" in t:
        found.add("ДТ")
    return found


def grade_inferred(st: dict[str, Any], grade: str) -> str:
    """Вспомогательный эвристический флаг только по текущему /api/stations.

    Не источник истины. Не смотрит историю комментариев.
    yes | no | unknown
    """
    parts = parse_grades(st.get("fuels_now"))
    if grade in parts:
        return "yes"
    status = st.get("status")
    if parts and grade not in parts and status in ("yes", "queue", "low"):
        return "no"
    if bool(st.get("dt_only")) and grade in ("92", "95", "98", "100") and status in (
        "yes",
        "queue",
        "low",
    ):
        return "no"
    return "unknown"


def collect_stations(
    client: GdeBenzClient,
    poly: list[PolyPoint],
    step_km: float,
    bbox_km: float,
    max_dist_km: float,
) -> dict[str, dict]:
    points = sample_polyline(poly, step_km)
    by_id: dict[str, dict] = {}
    print(f"probe points: {len(points)}", file=sys.stderr)
    for i, (lat, lon) in enumerate(points, start=1):
        lat1, lon1, lat2, lon2 = deg_box(lat, lon, bbox_km)
        print(f"[{i}/{len(points)}] {lat:.4f},{lon:.4f}", file=sys.stderr)
        stations = client.stations(lat1, lon1, lat2, lon2)
        for st in stations:
            oid = str(st.get("osm_id") or "")
            if not oid or not is_target_brand(st.get("brand"), st.get("name")):
                continue
            prev = by_id.get(oid)
            if prev is None or (prev.get("status") is None and st.get("status") is not None):
                by_id[oid] = st
        print(f"  bbox={len(stations)} unique_target={len(by_id)}", file=sys.stderr)

    near: dict[str, dict] = {}
    for oid, st in by_id.items():
        try:
            lat, lon = float(st["lat"]), float(st["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        dist, _ = dist_and_s_on_polyline(lat, lon, poly)
        if dist <= max_dist_km:
            near[oid] = st
    return near


def build_snapshot_station(
    st: dict[str, Any],
    poly: list[PolyPoint],
    recent_raw: list[dict],
) -> dict[str, Any]:
    lat, lon = float(st["lat"]), float(st["lon"])
    dist, route_km = dist_and_s_on_polyline(lat, lon, poly)

    # Полностью сырые комментарии API (без интерпретации).
    recent_comments = [json.loads(json.dumps(c, ensure_ascii=False)) for c in recent_raw]

    latest = recent_comments[0] if recent_comments else None
    latest_comment_status = latest.get("status") if latest else None
    latest_comment_detail = latest.get("detail") if latest else None
    latest_comment_at = latest.get("created_at") if latest else None

    last_detail = latest_comment_detail
    last_at = parse_api_time(latest_comment_at) if latest_comment_at else None
    if last_at is None and st.get("last_at") is not None:
        last_at = parse_api_time(st.get("last_at"))

    brand_raw = st.get("brand")
    item: dict[str, Any] = {
        "osm_id": str(st.get("osm_id")),
        "network": network_norm(st.get("brand"), st.get("name")),
        "brand_raw": brand_raw if brand_raw is not None else None,
        "name": st.get("name") if st.get("name") is not None else None,
        "address": st.get("addr") if st.get("addr") is not None else None,
        "lat": lat,
        "lon": lon,
        "route_km": round(route_km, 1),
        "distance_from_route_km": round(dist, 2),
        "status_raw": st.get("status") if st.get("status") is not None else None,
        "fuels_now_raw": st.get("fuels_now") if st.get("fuels_now") is not None else None,
        "last_at": last_at,
        "last_detail": last_detail,
        "latest_comment_status": latest_comment_status,
        "latest_comment_detail": latest_comment_detail,
        "latest_comment_at": latest_comment_at,
        # Вспомогательные эвристики; не источник истины для анализа.
        "ai95_inferred": grade_inferred(st, "95"),
        "ai100_inferred": grade_inferred(st, "100"),
        "meta": st.get("meta") if isinstance(st.get("meta"), dict) else None,
        "prices_now": st.get("prices_now") if isinstance(st.get("prices_now"), dict) else None,
        "conflict": st.get("conflict") if "conflict" in st else None,
        "dt_only": st.get("dt_only") if "dt_only" in st else None,
        "station_raw": json.loads(json.dumps(st, ensure_ascii=False)),
        "recent_comments": recent_comments,
    }
    return item


def write_reports(reports_dir: Path, snapshot: dict[str, Any], fetched_at: datetime) -> Path:
    history_dir = reports_dir / "history" / fetched_at.strftime("%Y-%m-%d")
    history_dir.mkdir(parents=True, exist_ok=True)
    rel_name = f"{fetched_at.strftime('%H%M')}Z.json"
    snapshot_path = history_dir / rel_name
    rel_path = f"history/{fetched_at.strftime('%Y-%m-%d')}/{rel_name}"

    text = json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n"
    snapshot_path.write_text(text, encoding="utf-8")
    (reports_dir / "latest.json").write_text(text, encoding="utf-8")

    index_path = reports_dir / "index.json"
    if index_path.exists():
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            index = {"latest": None, "updated_at": None, "snapshots": []}
    else:
        index = {"latest": None, "updated_at": None, "snapshots": []}

    snaps = [str(x) for x in index.get("snapshots") or []]
    if rel_path not in snaps:
        snaps.append(rel_path)
    snaps = sorted(set(snaps))
    index = {
        "latest": rel_path,
        "updated_at": to_iso_z(fetched_at),
        "snapshots": snaps,
    }
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return snapshot_path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Collect raw gdebenz snapshots along EK→NSK route")
    p.add_argument("--reports-dir", type=Path, default=Path("reports"))
    p.add_argument("--step-km", type=float, default=40.0)
    p.add_argument("--bbox-km", type=float, default=18.0)
    p.add_argument("--max-dist-km", type=float, default=15.0)
    p.add_argument("--pause", type=float, default=0.2)
    p.add_argument("--recent-limit", type=int, default=5)
    p.add_argument(
        "--skip-recent",
        action="store_true",
        help="не запрашивать /comments/{id}/recent (только для отладки)",
    )
    return p.parse_args()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    args = parse_args()
    fetched_at = utc_now()
    client = GdeBenzClient(pause_s=args.pause)

    print("fetch /api/rt", file=sys.stderr)
    rt = client.get_rt()
    if not rt:
        print("FAIL: empty /api/rt", file=sys.stderr)
        return 1
    # token только в памяти процесса, не пишем в snapshot/репозиторий
    print(f"rt ok (len={len(rt)})", file=sys.stderr)

    print("fetch OSRM route", file=sys.stderr)
    poly, osrm_meta = fetch_osrm_route()
    if len(poly) < 2 or osrm_meta["distance_km"] < 100:
        print("FAIL: invalid OSRM geometry", file=sys.stderr)
        return 1
    print(f"osrm {osrm_meta['distance_km']} km, points={osrm_meta['points']}", file=sys.stderr)

    print("collect /api/stations along route", file=sys.stderr)
    by_id = collect_stations(
        client,
        poly,
        step_km=args.step_km,
        bbox_km=args.bbox_km,
        max_dist_km=args.max_dist_km,
    )
    if len(by_id) < 20:
        print(f"FAIL: too few stations after filter: {len(by_id)}", file=sys.stderr)
        return 1

    stations_out: list[dict[str, Any]] = []
    ids = sorted(by_id.keys())
    print(f"enrich recent for {len(ids)} stations", file=sys.stderr)
    for i, oid in enumerate(ids, start=1):
        st = by_id[oid]
        recent: list[dict] = []
        if not args.skip_recent:
            try:
                recent = client.comments_recent(oid, limit=args.recent_limit)
            except Exception as exc:  # noqa: BLE001
                # отдельная АЗС не должна ронять весь snapshot, если stations уже собраны;
                # но пустой recent допустим только как [] при реальном ответе.
                # При ошибке оставляем [] и фиксируем в stderr.
                print(f"  warn recent {oid}: {exc}", file=sys.stderr)
                recent = []
        stations_out.append(build_snapshot_station(st, poly, recent))
        if i % 25 == 0:
            print(f"  {i}/{len(ids)}", file=sys.stderr)

    stations_out.sort(key=lambda x: (x.get("route_km") is None, x.get("route_km") or 0, x.get("osm_id") or ""))

    # sanity: route_km must be present and spread along route
    with_route = [s for s in stations_out if isinstance(s.get("route_km"), (int, float))]
    if len(with_route) < 20:
        print("FAIL: missing route_km on stations", file=sys.stderr)
        return 1
    max_route = max(float(s["route_km"]) for s in with_route)
    if max_route < osrm_meta["distance_km"] * 0.5:
        print(f"FAIL: route_km coverage too small ({max_route})", file=sys.stderr)
        return 1

    snapshot = {
        "fetched_at": to_iso_z(fetched_at),
        "source": BASE,
        "route": {
            "from": "Екатеринбург",
            "to": "Новосибирск",
            "waypoints": [w[0] for w in WAYPOINTS],
            "distance_km": osrm_meta["distance_km"],
            "duration_h": osrm_meta["duration_h"],
            "routing": osrm_meta["provider"],
        },
        "params": {
            "step_km": args.step_km,
            "bbox_km": args.bbox_km,
            "max_dist_km": args.max_dist_km,
        },
        "stations": stations_out,
    }

    path = write_reports(args.reports_dir, snapshot, fetched_at)
    nets: dict[str, int] = {}
    for s in stations_out:
        nets[s["network"]] = nets.get(s["network"], 0) + 1
    print(json.dumps({"stations": len(stations_out), "networks": nets, "path": str(path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
