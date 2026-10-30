# Query patterns

Each query runs as-is in the `sqlite3` shell or any SQLite client. Values written `:name` are parameters: bind them in your client, or replace them with a literal. Dates are `YYYY-MM-DD` strings on the local clock (see `local_date` in the README).

Location matching uses boxes in degrees: 0.01° of latitude is about 1.1 km, and a degree of longitude shrinks as you move away from the equator. The tolerances below are deliberately loose; tighten them for your use.

## Day by day

One row per day with visits, distinct places, minutes at places, whether a home visit began that day, and kilometers traveled (each visit counts toward the day it started; the mobility-entropy query below shows how to split visits at midnight when you need exact per-day time):
```sql
SELECT local_date, visit_count, distinct_places, visit_minutes, touched_home, travel_km
FROM v_days
WHERE local_date BETWEEN :from_date AND :to_date
ORDER BY local_date;
```

Nights away from home (a day with visits but no visit starting at the place Google labeled HOME or INFERRED_HOME; since a visit belongs to the day it started, a day you left home and never came back to it reads as a night away):
```sql
SELECT local_date FROM v_days
WHERE visit_count > 0 AND touched_home = 0
ORDER BY local_date;
```

First time out and last arrival home, per day:
```sql
SELECT local_date,
       MIN(CASE WHEN semantic_type NOT IN ('HOME','INFERRED_HOME') THEN start_time END) AS first_out,
       MAX(CASE WHEN semantic_type IN ('HOME','INFERRED_HOME') THEN start_time END)     AS last_home_arrival
FROM visits
GROUP BY local_date
ORDER BY local_date;
```

## Places

Was I near a place on a given date? Supply the place's coordinates:
```sql
SELECT start_time, end_time, duration_min, lat, lng
FROM visits
WHERE local_date = :date
  AND ABS(lat - :place_lat) < 0.015 AND ABS(lng - :place_lng) < 0.020
ORDER BY start_time;
```

Every visit longer than 20 minutes at one place (useful for counting how often you went somewhere, such as a gym):
```sql
SELECT local_date, start_time, duration_min
FROM visits
WHERE ABS(lat - :place_lat) < 0.002 AND ABS(lng - :place_lng) < 0.003
  AND duration_min > 20
ORDER BY start_time;
```

Most-visited places in a window, by total time:
```sql
SELECT place_id, COUNT(*) AS visits, SUM(duration_min) AS minutes,
       ROUND(AVG(lat), 4) AS lat, ROUND(AVG(lng), 4) AS lng
FROM visits
WHERE local_date BETWEEN :from_date AND :to_date
GROUP BY place_id
ORDER BY minutes DESC
LIMIT 25;
```

Where was I at a given moment? Pass a timestamp in the same form the export uses, with its offset. `julianday` turns each offset timestamp into one instant, so the comparison holds across time zones and finds visits that started the day before:
```sql
SELECT * FROM visits
WHERE julianday(start_time) <= julianday(:moment)
  AND julianday(end_time)   >= julianday(:moment);
```

## Trips and anniversaries

Days away from home, in order (group consecutive dates into trips in your own code):
```sql
SELECT local_date FROM v_days
WHERE touched_home = 0 AND visit_count > 0
ORDER BY local_date;
```

On this day: where you were on today's month and day in earlier years:
```sql
SELECT local_date, start_time, duration_min, lat, lng, semantic_type
FROM visits
WHERE substr(local_date, 6) = strftime('%m-%d', 'now', 'localtime')
ORDER BY local_date;
```

## Mobility entropy

How evenly your time was spread across places over the 28 days ending on `:end_date`, from 0 (all time at one place) to 1 (time split evenly across every place you visited). The query returns nothing unless at least 20 of the 28 days have visits and at least 2 places appear, because a sparse window gives a misleading number. It counts only top-level visits (`hierarchy_level = 0`) and splits visits that cross midnight between the days they cover. Timestamps are compared on their local-clock part.
```sql
WITH RECURSIVE
params(end_date, window_start) AS (
  SELECT date(:end_date), date(:end_date, '-27 days')
),
days(day) AS (
  SELECT window_start FROM params
  UNION ALL
  SELECT date(day, '+1 day') FROM days, params WHERE day < end_date
),
overlaps AS (
  SELECT d.day, v.place_id,
         (julianday(MIN(substr(v.end_time, 1, 19), date(d.day, '+1 day') || 'T00:00:00'))
        - julianday(MAX(substr(v.start_time, 1, 19), d.day || 'T00:00:00'))) * 1440.0 AS minutes
  FROM days d JOIN visits v
    ON v.hierarchy_level = 0
   AND substr(v.end_time, 1, 19) > d.day || 'T00:00:00'
   AND substr(v.start_time, 1, 19) < date(d.day, '+1 day') || 'T00:00:00'
),
coverage AS (
  SELECT COUNT(DISTINCT day) AS covered_days FROM overlaps WHERE minutes > 0
),
per_place AS (
  SELECT place_id, SUM(minutes) AS minutes FROM overlaps
  WHERE minutes > 0 AND place_id IS NOT NULL GROUP BY place_id
),
totals AS (
  SELECT SUM(minutes) AS total_minutes, COUNT(*) AS n_places FROM per_place
)
SELECT params.end_date, coverage.covered_days, totals.n_places,
       ROUND(-SUM((minutes / total_minutes) * LN(minutes / total_minutes))
             / LN(n_places), 3) AS mobility_entropy
FROM params, coverage, totals, per_place
GROUP BY params.end_date, coverage.covered_days, totals.n_places
HAVING covered_days >= 20 AND n_places >= 2;
```
`LN` needs SQLite 3.35 or newer built with math functions (the default in recent Python and sqlite3 shell builds).
