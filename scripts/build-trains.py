#!/usr/bin/env python3
"""
Builds public/transit/trains.json: the Israel Railways timetable for the next DAYS days, taken from
the Ministry of Transport's official GTFS feed (https://gtfs.mot.gov.il, the same data every Israeli
transit app uses). The app plans train trips on the phone from this file, with no server and no paid
API. Copy of mathu79/MahFamily scripts/build-trains.py. This public repo runs it twice a week
(.github/workflows/trains.yml, free because the repo is public) and commits transit/trains.json;
https://mahfamily-fd2ab.web.app/transit/trains.json redirects here. On failure the last commit stays.

Output (compact, ~200 KB):
  { "generated": ISO time, "days": ["YYYYMMDD", ...],          # day i of every mask = days[i]
    "stations": [[name, lat, lng], ...],
    "trips": [[mask, train number, headsign, [station, arr, dep, station, arr, dep, ...]], ...] }
arr/dep are minutes after midnight of the service day (can pass 1440 after midnight).

Usage: python3 scripts/build-trains.py [path-or-url-of-gtfs-zip]
"""
import csv
import datetime as dt
import io
import json
import os
import sys
import tempfile
import urllib.request
import zipfile

URL = "https://gtfs.mot.gov.il/gtfsfiles/israel-public-transportation.zip"
OUT = os.environ.get("TRAINS_OUT") or os.path.join(os.path.dirname(__file__), "..", "transit", "trains.json")
DAYS = 14
RAIL_AGENCY_NAMES = ("רכבת ישראל",)


def rows(z, name):
    with z.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))


def minutes(t):
    h, m, _s = t.strip().split(":")
    return int(h) * 60 + int(m)


def fetch(src):
    if os.path.exists(src):
        return src
    path = os.path.join(tempfile.gettempdir(), "mot-gtfs.zip")
    print("downloading", src, flush=True)
    req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0 (MahFamily timetable build)"})
    with urllib.request.urlopen(req, timeout=600) as r, open(path, "wb") as out:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
    print("downloaded", os.path.getsize(path) // (1 << 20), "MB", flush=True)
    return path


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else URL
    z = zipfile.ZipFile(fetch(src))
    names = set(z.namelist())

    agencies = {a["agency_id"] for a in rows(z, "agency.txt") if a["agency_name"].strip() in RAIL_AGENCY_NAMES}
    routes = {
        r["route_id"]: r
        for r in rows(z, "routes.txt")
        if r["agency_id"] in agencies or r.get("route_type", "").strip() == "2"
    }
    print("rail routes:", len(routes), flush=True)

    trips = {}
    for t in rows(z, "trips.txt"):
        if t["route_id"] in routes:
            trips[t["trip_id"]] = t
    print("rail trips:", len(trips), "sample:", next(iter(trips.values()), None), flush=True)

    # Which of the next DAYS days each service runs on (Israel time).
    today = (dt.datetime.utcnow() + dt.timedelta(hours=2)).date()
    days = [today + dt.timedelta(days=i) for i in range(DAYS)]
    keys = [d.strftime("%Y%m%d") for d in days]
    weekday_cols = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    services = {t["service_id"] for t in trips.values()}
    masks = {}
    if "calendar.txt" in names:
        for c in rows(z, "calendar.txt"):
            sid = c["service_id"]
            if sid not in services:
                continue
            m = 0
            for i, d in enumerate(days):
                k = keys[i]
                if c["start_date"] <= k <= c["end_date"] and c[weekday_cols[d.weekday()]] == "1":
                    m |= 1 << i
            masks[sid] = m
    if "calendar_dates.txt" in names:
        for c in rows(z, "calendar_dates.txt"):
            sid = c["service_id"]
            if sid not in services or c["date"] not in keys:
                continue
            bit = 1 << keys.index(c["date"])
            masks[sid] = (masks.get(sid, 0) | bit) if c["exception_type"] == "1" else (masks.get(sid, 0) & ~bit)

    stop_times = {}
    for st in rows(z, "stop_times.txt"):
        tid = st["trip_id"]
        if tid in trips:
            stop_times.setdefault(tid, []).append(
                (int(st["stop_sequence"]), st["stop_id"], minutes(st["arrival_time"]), minutes(st["departure_time"]))
            )
    print("rail stop times:", sum(len(v) for v in stop_times.values()), flush=True)

    used = {s for v in stop_times.values() for _, s, _, _ in v}
    stops = {s["stop_id"]: s for s in rows(z, "stops.txt") if s["stop_id"] in used}
    # Platforms of one station share a parent: show and route them as one station.
    parents = {}
    for s in list(stops.values()):
        p = s.get("parent_station", "").strip()
        if p:
            parents[s["stop_id"]] = p
    if parents:
        for s in rows(z, "stops.txt"):
            if s["stop_id"] in parents.values():
                stops[s["stop_id"]] = s

    def station_of(stop_id):
        return parents.get(stop_id, stop_id)

    station_ids = sorted({station_of(s) for s in used}, key=lambda s: stops[s]["stop_name"])
    index = {s: i for i, s in enumerate(station_ids)}
    stations = [
        [stops[s]["stop_name"].strip().replace("''", "״"), round(float(stops[s]["stop_lat"]), 5), round(float(stops[s]["stop_lon"]), 5)]
        for s in station_ids
    ]

    out_trips = {}
    for tid, sts in stop_times.items():
        t = trips[tid]
        m = masks.get(t["service_id"], 0)
        if not m:
            continue
        sts.sort()
        seq = []
        for _, s, a, d in sts:
            seq += [index[station_of(s)], a, d]
        number = (t.get("trip_short_name") or "").strip()
        headsign = (t.get("trip_headsign") or "").strip()
        # The ministry's feed puts the train number in the headsign.
        if not number and headsign.isdigit():
            number, headsign = headsign, ""
        key = (number, headsign, tuple(seq))
        out_trips[key] = out_trips.get(key, 0) | m

    result = {
        "generated": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "source": "משרד התחבורה - GTFS",
        "days": keys,
        "stations": stations,
        "trips": sorted(
            ([m, n, h, list(seq)] for (n, h, seq), m in out_trips.items()), key=lambda t: (t[3][1], t[1])
        ),
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))
    print("stations:", len(stations), "trips:", len(result["trips"]), "bytes:", os.path.getsize(OUT))
    if len(stations) < 40 or len(result["trips"]) < 200:
        sys.exit("too little rail data, something is off")


if __name__ == "__main__":
    main()
