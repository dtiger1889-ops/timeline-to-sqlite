"""Transform a Google Maps Timeline export (Timeline.json, on-device format) into timeline.db.

Idempotent: drops and rebuilds every table from the given export. Python standard library only.

Usage:
    python transform_timeline.py <path-to-Timeline.json> [--db <output.db>]

--db defaults to timeline.db next to this script.
"""
import argparse
import json
import os
import sqlite3
import sys

SCHEMA = """
DROP TABLE IF EXISTS visits;
CREATE TABLE visits (
    id INTEGER PRIMARY KEY,
    start_time TEXT NOT NULL,       -- ISO8601 with local offset, as exported
    end_time TEXT NOT NULL,
    local_date TEXT NOT NULL,       -- YYYY-MM-DD of start_time's local clock
    duration_min REAL,
    lat REAL, lng REAL,
    place_id TEXT,
    semantic_type TEXT,             -- HOME / WORK / SEARCHED_ADDRESS / UNKNOWN ...
    probability REAL,               -- visit-level confidence
    place_probability REAL,         -- top place-candidate confidence
    hierarchy_level INTEGER
);
DROP TABLE IF EXISTS activities;
CREATE TABLE activities (
    id INTEGER PRIMARY KEY,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    local_date TEXT NOT NULL,
    duration_min REAL,
    start_lat REAL, start_lng REAL,
    end_lat REAL, end_lng REAL,
    type TEXT,                      -- WALKING / IN_PASSENGER_VEHICLE / ...
    probability REAL,
    distance_m REAL
);
DROP TABLE IF EXISTS path_points;
CREATE TABLE path_points (
    id INTEGER PRIMARY KEY,
    segment_start TEXT NOT NULL,    -- parent segment's startTime (groups a route)
    time TEXT NOT NULL,
    local_date TEXT NOT NULL,
    lat REAL, lng REAL
);
DROP TABLE IF EXISTS raw_positions;
CREATE TABLE raw_positions (
    id INTEGER PRIMARY KEY,
    time TEXT NOT NULL,
    local_date TEXT NOT NULL,
    lat REAL, lng REAL,
    accuracy_m REAL,
    altitude_m REAL,
    speed_mps REAL,
    source TEXT                     -- WIFI / GPS / CELL ...
);
DROP TABLE IF EXISTS frequent_places;
CREATE TABLE frequent_places (
    place_id TEXT PRIMARY KEY,
    lat REAL, lng REAL,
    label TEXT                      -- HOME / WORK / NULL
);
DROP TABLE IF EXISTS meta;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);

CREATE INDEX idx_visits_date ON visits(local_date);
CREATE INDEX idx_visits_place ON visits(place_id);
CREATE INDEX idx_visits_latlng ON visits(lat, lng);
CREATE INDEX idx_activities_date ON activities(local_date);
CREATE INDEX idx_path_date ON path_points(local_date);
CREATE INDEX idx_raw_date ON raw_positions(local_date);

DROP VIEW IF EXISTS v_days;
CREATE VIEW v_days AS
SELECT local_date,
       COUNT(*)                                   AS visit_count,
       COUNT(DISTINCT place_id)                   AS distinct_places,
       SUM(duration_min)                          AS visit_minutes,
       MAX(CASE WHEN semantic_type='HOME' THEN 1 ELSE 0 END) AS touched_home,
       (SELECT ROUND(SUM(distance_m)/1000.0, 1) FROM activities a
         WHERE a.local_date = v.local_date)       AS travel_km
FROM visits v
GROUP BY local_date;
"""


def parse_latlng(s):
    """A "<lat>°, <lng>°" string -> (lat, lng) floats; also accepts a dict holding latLng/LatLng.
    Returns (None, None) when the value is missing or unparseable."""
    if s is None:
        return (None, None)
    if isinstance(s, dict):
        s = s.get("latLng") or s.get("LatLng")
        if s is None:
            return (None, None)
    try:
        lat_s, lng_s = s.split(",")
        return (float(lat_s.replace("°", "").strip()),
                float(lng_s.replace("°", "").strip()))
    except (ValueError, AttributeError):
        return (None, None)


def local_date(iso):
    return iso[:10] if iso else None


def duration_min(start, end):
    """Minutes between two ISO strings; robust to offsets via datetime."""
    from datetime import datetime
    try:
        s = datetime.fromisoformat(start)
        e = datetime.fromisoformat(end)
        return round((e - s).total_seconds() / 60.0, 1)
    except (ValueError, TypeError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("json_path")
    ap.add_argument("--db", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                 "timeline.db"))
    args = ap.parse_args()

    print(f"Loading {args.json_path} ...")
    with open(args.json_path, encoding="utf-8") as f:
        data = json.load(f)

    con = sqlite3.connect(args.db)
    con.executescript(SCHEMA)

    n_visit = n_act = n_path = n_mem = 0
    for seg in data.get("semanticSegments", []):
        st, et = seg.get("startTime"), seg.get("endTime")
        if "visit" in seg:
            v = seg["visit"]
            top = v.get("topCandidate", {})
            lat, lng = parse_latlng(top.get("placeLocation"))
            con.execute(
                "INSERT INTO visits (start_time,end_time,local_date,duration_min,lat,lng,"
                "place_id,semantic_type,probability,place_probability,hierarchy_level) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (st, et, local_date(st), duration_min(st, et), lat, lng,
                 top.get("placeId"), top.get("semanticType"), v.get("probability"),
                 top.get("probability"), v.get("hierarchyLevel")))
            n_visit += 1
        elif "activity" in seg:
            a = seg["activity"]
            top = a.get("topCandidate", {})
            slat, slng = parse_latlng(a.get("start"))
            elat, elng = parse_latlng(a.get("end"))
            con.execute(
                "INSERT INTO activities (start_time,end_time,local_date,duration_min,"
                "start_lat,start_lng,end_lat,end_lng,type,probability,distance_m) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (st, et, local_date(st), duration_min(st, et), slat, slng, elat, elng,
                 top.get("type"), top.get("probability"), a.get("distanceMeters")))
            n_act += 1
        elif "timelinePath" in seg:
            rows = []
            for p in seg["timelinePath"]:
                lat, lng = parse_latlng(p.get("point"))
                t = p.get("time")
                rows.append((st, t, local_date(t), lat, lng))
            con.executemany(
                "INSERT INTO path_points (segment_start,time,local_date,lat,lng) "
                "VALUES (?,?,?,?,?)", rows)
            n_path += len(rows)
        else:
            n_mem += 1  # timelineMemory (trip summaries) and unknown types: counted, not stored

    n_raw = 0
    for sig in data.get("rawSignals", []):
        pos = sig.get("position")
        if not pos:
            continue  # wifiScan / activityRecord signals carry no position
        lat, lng = parse_latlng(pos.get("LatLng") or pos.get("latLng"))
        t = pos.get("timestamp")
        con.execute(
            "INSERT INTO raw_positions (time,local_date,lat,lng,accuracy_m,altitude_m,"
            "speed_mps,source) VALUES (?,?,?,?,?,?,?,?)",
            (t, local_date(t), lat, lng, pos.get("accuracyMeters"),
             pos.get("altitudeMeters"), pos.get("speedMetersPerSecond"), pos.get("source")))
        n_raw += 1

    for fp in (data.get("userLocationProfile") or {}).get("frequentPlaces", []):
        lat, lng = parse_latlng(fp.get("placeLocation"))
        con.execute("INSERT OR REPLACE INTO frequent_places (place_id,lat,lng,label) "
                    "VALUES (?,?,?,?)", (fp.get("placeId"), lat, lng, fp.get("label")))

    con.execute("INSERT INTO meta VALUES ('source_file', ?)", (args.json_path,))
    con.execute("INSERT INTO meta VALUES ('transformed_counts', ?)",
                (json.dumps({"visits": n_visit, "activities": n_act, "path_points": n_path,
                             "raw_positions": n_raw, "skipped_segments": n_mem}),))
    con.commit()

    print(f"visits={n_visit} activities={n_act} path_points={n_path} "
          f"raw_positions={n_raw} skipped(memory)={n_mem}")
    lo, hi = con.execute("SELECT MIN(local_date), MAX(local_date) FROM visits").fetchone()
    print(f"visit date span: {lo} .. {hi}")
    con.close()
    print(f"OK -> {args.db}")


if __name__ == "__main__":
    main()
