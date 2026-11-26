"""Point-in-time KNYC forecasts and observations for paper research."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc
STATION = "KNYC"
AWC_MODEL = "NOAA Aviation Weather Center KNYC METAR"
METAR_MAX_AGE = timedelta(hours=2)
AUTHORITY = "CLINYC/The Weather Company/GLOBALTEMPERATURE"
MODEL = "KNYC empirical error v1"
MIN_DAYS = 30
SEASON_DAYS = 45
POINT_MODEL = "NWS issued daytime high"
NBM_MODEL = "NOAA NBM NBS 3-hour maximum"
FORECAST_TTL = {NBM_MODEL: 42 * 3600, POINT_MODEL: 18 * 3600}
SAME_AS_ISSUE = object()

try:
    EASTERN = ZoneInfo("America/New_York")
except ZoneInfoNotFoundError:  # A stock Windows Python may have no IANA database.
    EASTERN = None


def instant(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("A source timestamp is missing")
    value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("A source timestamp has no timezone")
    return value.astimezone(UTC)


def iso(value: datetime) -> str:
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def eastern(value: datetime) -> datetime:
    if EASTERN is not None:
        return value.astimezone(EASTERN)
    # US Eastern rules since 2007, for the supported modern contract series.
    year = value.year
    march = date(year, 3, 8)
    november = date(year, 11, 1)
    spring = datetime.combine(
        march + timedelta(days=(6 - march.weekday()) % 7), time(7), UTC
    )
    fall = datetime.combine(
        november + timedelta(days=(6 - november.weekday()) % 7), time(6), UTC
    )
    return value.astimezone(
        timezone(timedelta(hours=-4 if spring <= value < fall else -5))
    )


def midnight(day: date) -> datetime:
    utc_day = datetime.combine(day, time.min, UTC)
    return utc_day - eastern(utc_day + timedelta(hours=4)).utcoffset()


def event_end(day: date) -> datetime:
    return midnight(day + timedelta(days=1))


def event_day(event: str) -> date:
    hit = re.fullmatch(r"KXHIGHNY-(\d\d)([A-Z]{3})(\d\d)", event or "")
    if not hit:
        raise ValueError("Unrecognized KXHIGHNY event date")
    months = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()
    return date(2000 + int(hit[1]), months.index(hit[2]) + 1, int(hit[3]))


def evidence(title, url, published, available, detail, **extra):
    return {
        "title": title,
        "url": url,
        "published_at": published,
        "available_at": available,
        "retrieved_at": available,
        "detail": detail,
        **extra,
    }


def initialize(db: sqlite3.Connection):
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS weather_records(
            kind TEXT, event_day TEXT, model TEXT, issued TEXT, first_seen TEXT,
            published TEXT, url TEXT, value REAL, detail TEXT, digest TEXT, artifact TEXT,
            PRIMARY KEY(kind,event_day,model,issued,digest));
        CREATE INDEX IF NOT EXISTS forecasts_by_day ON weather_records(kind,event_day,first_seen);
        CREATE TABLE IF NOT EXISTS venue_highs(
            event_day TEXT, authority TEXT, high REAL, first_seen TEXT, resolved TEXT, source TEXT,
            PRIMARY KEY(event_day, authority, high));
        CREATE TABLE IF NOT EXISTS estimates(
            market_id TEXT, as_of TEXT, rule_version TEXT, estimate TEXT,
            PRIMARY KEY(market_id,as_of,rule_version));
    """
    )


def save(
    db,
    kind,
    day,
    model,
    issued,
    seen,
    url,
    value,
    detail,
    artifact,
    published=SAME_AS_ISSUE,
):
    if issued > seen:
        raise ValueError("A weather input has a future issuance time")
    raw = (
        artifact
        if isinstance(artifact, str)
        else json.dumps(artifact, sort_keys=True, separators=(",", ":"))
    )
    digest = hashlib.sha256(raw.encode()).hexdigest()
    published = issued if published is SAME_AS_ISSUE else published
    if published is not None and published > seen:
        raise ValueError("A weather input has a future publication time")
    db.execute(
        """INSERT OR IGNORE INTO weather_records
        VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            kind,
            day.isoformat(),
            model,
            iso(issued),
            iso(seen),
            iso(published) if published else None,
            url,
            value,
            detail,
            digest,
            raw,
        ),
    )


def read_point(db, data, url, seen):
    props = data["properties"]
    updated = instant(props["updateTime"])
    published = instant(props["generatedAt"])
    if updated > published or published > seen:
        raise ValueError(
            "NWS point forecast has inconsistent issue and generation times"
        )
    for period in props["periods"]:
        if period.get("isDaytime") is not True or period.get("temperatureUnit") != "F":
            continue
        start = instant(period["startTime"])
        end = instant(period["endTime"])
        high = period.get("temperature")
        if type(high) is not int or not -80 <= high <= 155 or end <= start:
            continue
        day = eastern(start).date()
        save(
            db,
            "forecast",
            day,
            POINT_MODEL,
            updated,
            seen,
            url,
            high,
            f"The issued NWS point forecast gives the Central Park grid cell a daytime high of {high}°F.",
            {
                "updateTime": props["updateTime"],
                "generatedAt": props["generatedAt"],
                "period": period,
            },
            published=published,
        )


def read_observation(db, data, url, seen):
    props = data["properties"]
    if props.get("station", "").rstrip("/").split("/")[-1] != STATION:
        raise ValueError("Observation station is not KNYC")
    issued = instant(props["timestamp"])
    reading = props["temperature"]
    if reading.get("unitCode") != "wmoUnit:degC" or reading.get(
        "qualityControl"
    ) not in {"V", "S", "C", "Z"}:
        raise ValueError("KNYC observation lacks a usable Celsius reading")
    value = reading["value"]
    if type(value) not in (float, int) or not -60 <= value <= 60:
        raise ValueError("KNYC observation has no usable temperature")
    fahrenheit = round(value * 9 / 5 + 32, 1)
    # The observation time is known. NWS does not supply a publication time here.
    save(
        db,
        "observation",
        eastern(issued).date(),
        STATION,
        issued,
        seen,
        url,
        fahrenheit,
        f"KNYC station observation {fahrenheit:g}°F at {iso(issued)}. A point reading within the day.",
        {
            "timestamp": props["timestamp"],
            "temperature": reading,
            "station": props["station"],
        },
        published=None,
    )


def read_metar(db, report, source, seen):
    """Archive one NOAA relay of the Central Park sensor, preserving local first receipt."""
    if not isinstance(report, dict) or report.get("icaoId") != STATION:
        raise ValueError("Aviation report has the wrong station.")
    timestamp = report.get("obsTime")
    if type(timestamp) is not int or not 0 < timestamp <= seen.timestamp():
        raise ValueError("Aviation observation time is missing or in the future.")
    observed = datetime.fromtimestamp(timestamp, UTC)
    try:
        received = instant(report.get("receiptTime"))
    except ValueError as exc:
        raise ValueError("Aviation provider receipt time is invalid.") from exc
    if not observed <= received <= seen:
        raise ValueError("Aviation receipt and observation times disagree.")
    if seen - observed > METAR_MAX_AGE:
        raise ValueError("The latest KNYC aviation report is over two hours old.")
    kind = report.get("metarType")
    raw = report.get("rawOb")
    if kind not in {"METAR", "SPECI"} or not isinstance(raw, str) or len(raw) > 1500:
        raise ValueError("Unrecognized KNYC aviation report.")
    raw = " ".join(raw.split())
    if not re.match(rf"^(?:{kind} )?KNYC {observed:%d%H%M}Z(?: |$)", raw):
        raise ValueError("Raw aviation report station or observation time disagrees.")
    celsius = report.get("temp")
    if type(celsius) not in (float, int) or not -60 <= celsius <= 60:
        raise ValueError("Aviation report lacks a usable Celsius temperature.")
    # A retransmission may update the provider's receipt time for the same raw
    # observation. A corrected raw report at the same measured time is distinct.
    if db.execute(
        """SELECT 1 FROM weather_records WHERE kind='observation' AND model=? AND issued=?
           AND json_extract(artifact,'$.rawOb')=? LIMIT 1""",
        (AWC_MODEL, iso(observed), raw),
    ).fetchone():
        return
    fahrenheit = round(celsius * 9 / 5 + 32, 1)
    detail = [
        f"{'Special SPECI' if kind == 'SPECI' else 'Routine METAR'} report from Central Park measured "
        f"{fahrenheit:g}°F at {observed:%Y-%m-%d %H:%M UTC}."
    ]
    speed, gust = report.get("wspd"), report.get("wgst")
    if type(speed) in (int, float) and 0 <= speed <= 250:
        wind = f"Wind {speed:g} kt"
        if type(gust) in (int, float) and speed <= gust <= 300:
            wind += f", gusting to {gust:g} kt"
        detail.append(wind + ".")
    detail.append(
        f"NOAA Aviation received it at {received:%H:%M:%S UTC}. "
        "This is the same KNYC sensor used by the NWS station feed."
    )
    # reportTime can be a nominal hour after receipt for routine METARs.
    # Neither it nor NOAA's upstream receipt establishes a publication time.
    artifact = {
        k: report.get(k)
        for k in (
            "icaoId",
            "obsTime",
            "receiptTime",
            "reportTime",
            "metarType",
            "temp",
            "wspd",
            "wgst",
        )
    }
    artifact["rawOb"] = raw
    save(
        db,
        "observation",
        eastern(observed).date(),
        AWC_MODEL,
        observed,
        seen,
        source + "&date=" + observed.strftime("%Y-%m-%dT%H:%M:%SZ"),
        fahrenheit,
        " ".join(detail),
        artifact,
        published=None,
    )


def read_cli(db, data, url, seen):
    body = data["productText"]
    issued = instant(data["issuanceTime"])
    if (
        data.get("issuingOffice") != "KOKX"
        or data.get("productCode") != "CLI"
        or not re.search(r"^CLINYC\s*$", body, re.M)
    ):
        raise ValueError(
            "Unexpected station or product in Central Park climate bulletin"
        )
    match = re.search(
        r"CENTRAL PARK (?:NY )?CLIMATE SUMMARY FOR ([A-Z]+) (\d{1,2}) (\d{4})", body
    )
    temp = re.search(
        r"TEMPERATURE \(F\)[^\n]*\n\s*(TODAY|YESTERDAY)\s*\n\s*MAXIMUM\s+(-?\d+)\b",
        body,
    )
    if not match or not temp or not -80 <= int(temp[2]) <= 155:
        raise ValueError(
            "Central Park climate bulletin lacks a usable dated daily maximum"
        )
    month_names = "JANUARY FEBRUARY MARCH APRIL MAY JUNE JULY AUGUST SEPTEMBER OCTOBER NOVEMBER DECEMBER".split()
    day = date(int(match[3]), month_names.index(match[1]) + 1, int(match[2]))
    if (
        temp[1] == "TODAY"
        and "VALID TODAY AS OF" in body
        and eastern(issued).date() == day
    ):
        kind = "preliminary"
        description = "Preliminary Central Park NWS climate report"
    elif (
        temp[1] == "YESTERDAY"
        and "VALID TODAY AS OF" not in body
        and eastern(issued).date() > day
    ):
        kind = "complete"
        description = "Completed-day Central Park NWS climate report"
    else:
        raise ValueError("Central Park climate bulletin coverage is ambiguous")
    high = int(temp[2])
    save(
        db,
        kind,
        day,
        "CLINYC",
        issued,
        seen,
        url,
        high,
        f"{description} for {day.isoformat()} gives a maximum of {high}°F. Independent weather evidence.",
        body,
    )


def read_nbm(
    db, card: str, url: str, run: datetime, published: datetime, seen: datetime
):
    """Use eight (seven/nine around DST as applicable) 3-hour slots, never a partial day."""
    header = re.search(
        r"^\s*KNYC\s+NBM(?:\s+(V[\d.]+))?\s+NBS GUIDANCE\s+(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{2})00 UTC",
        card,
    )
    if not header:
        raise ValueError("NBM station header or guidance type changed")
    issued = datetime(
        int(header[4]), int(header[2]), int(header[3]), int(header[5]), tzinfo=UTC
    )
    if issued != run or not issued <= published <= seen:
        raise ValueError("NBM run, publication and receipt times disagree")

    def values(label):
        row = re.search(r"^\s*" + label + r" +([^\n]+)$", card, re.M)
        if not row:
            raise ValueError("NBM station guidance is missing " + label)
        return [int(v) for v in row[1].split()]

    leads = values("FHR")
    utc_hours = values("UTC")
    temps = values("TMP")
    if not leads or not len(temps) == len(leads) == len(utc_hours):
        raise ValueError("NBM station guidance columns disagree")
    by_day = {}
    for lead, hour, high in zip(leads, utc_hours, temps):
        valid = issued + timedelta(hours=lead)
        if lead < 0 or hour != valid.hour or hour % 3:
            raise ValueError("Unexpected NBM station guidance time grid")
        if -80 <= high <= 155:
            by_day.setdefault(eastern(valid).date(), {})[valid] = high
    for day, samples in by_day.items():
        start = midnight(day)
        # The UTC grid is fixed. Civil-day coverage changes around DST.
        first = start + timedelta(hours=(-start.hour) % 3)
        expected = set()
        while first < event_end(day):
            expected.add(first)
            first += timedelta(hours=3)
        if samples.keys() != expected:
            continue
        high = max(samples.values())
        version = header[1] or "operational, version omitted from header"
        save(
            db,
            "forecast",
            day,
            NBM_MODEL,
            issued,
            seen,
            url,
            high,
            f"The NOAA KNYC NBS {run:%HZ} run ({version}) peaks at {high}°F across all "
            f"{len(expected)} three-hour samples on {day.isoformat()}.",
            card,
            published=published,
        )


def records(db, asof, day=None, limit=12):
    if day is None:
        # New point-forecast periods should not push both climate products and
        # the station observation off the worker's short source-health timeline.
        rows = db.execute(
            """WITH per_day AS (
            SELECT *,ROW_NUMBER() OVER (PARTITION BY kind,event_day,model ORDER BY issued DESC,first_seen DESC) n
            FROM weather_records WHERE first_seen<=?),
            per_source AS (SELECT *,ROW_NUMBER() OVER(PARTITION BY kind,model ORDER BY issued DESC,event_day ASC) rank
            FROM per_day WHERE n=1)
            SELECT * FROM per_source ORDER BY rank,issued DESC LIMIT ?""",
            (iso(asof), limit),
        )
    else:
        # For tomorrow's contract the latest station report describes conditions
        # today. Keep its actual event date and prefer one record per source
        # before filling remaining slots with older records.
        newest = iso(asof - METAR_MAX_AGE)
        today = eastern(asof).date()
        include_today = str(day) == str(today + timedelta(days=1))
        rows = db.execute(
            """WITH dated AS (
            SELECT *,ROW_NUMBER() OVER (PARTITION BY kind,model ORDER BY issued DESC,first_seen DESC) rank
            FROM weather_records WHERE first_seen<=? AND
                ((event_day=? AND (model<>? OR issued>=?))
                 OR (? AND event_day=? AND model=? AND issued>=?)))
            SELECT * FROM dated WHERE model<>? OR rank=1
            ORDER BY rank,issued DESC,first_seen DESC LIMIT ?""",
            (
                iso(asof),
                str(day),
                AWC_MODEL,
                newest,
                include_today,
                str(today),
                AWC_MODEL,
                newest,
                AWC_MODEL,
                limit,
            ),
        )
    labels = {
        "forecast": "Issued forecast",
        "preliminary": "Preliminary NWS bulletin",
        "complete": "Completed-day NWS bulletin",
        "observation": "Station observation",
    }
    return [
        evidence(
            (
                "NOAA Aviation Weather Center · KNYC"
                if r["model"] == AWC_MODEL
                else labels[r["kind"]]
            ),
            r["url"],
            r["published"],
            r["first_seen"],
            r["detail"],
            event_date=r["event_day"],
            kind=r["kind"],
            issued_at=r["issued"],
            context_only=r["kind"] != "forecast",
            sha256=r["digest"],
            **(
                {
                    "source_received_at": json.loads(r["artifact"])["receiptTime"],
                    "station": STATION,
                }
                if r["model"] == AWC_MODEL
                else {}
            ),
        )
        for r in rows
    ]


def qualifies(value, bounds):
    lo, hi = bounds
    return (lo is None or value >= lo) and (hi is None or value <= hi)


def season_distance(first, second):
    # Same fixed leap year for a circular calendar-day window across New Year.
    difference = abs((first.replace(year=2000) - second.replace(year=2000)).days)
    return min(difference, 366 - difference)


def source_version(row):
    if row["model"] == NBM_MODEL:
        version = re.search(r"KNYC\s+NBM\s+(V[\d.]+)\s+NBS GUIDANCE", row["artifact"])
        return (
            version[1]
            if version
            else "NBS operational bulletin, upstream version unspecified"
        )
    return row["model"]


def estimate(db, day, bounds, asof):
    result = dict(
        probability=None,
        lower=None,
        upper=None,
        baseline=None,
        model=MODEL,
        samples=0,
        issued_at=None,
        minimum_samples=MIN_DAYS,
        source=None,
    )
    stamp = iso(asof)
    forecasts = db.execute(
        """SELECT * FROM weather_records WHERE kind='forecast' AND event_day=?
        AND issued<=? AND first_seen<=? AND published<=?
        ORDER BY CASE model WHEN ? THEN 0 ELSE 1 END, issued DESC, first_seen DESC""",
        (day.isoformat(), stamp, stamp, stamp, NBM_MODEL),
    )
    current = next(
        (
            r
            for r in forecasts
            if (asof - instant(r["issued"])).total_seconds() <= FORECAST_TTL[r["model"]]
        ),
        None,
    )
    if not current:
        return result, "Awaiting a fresh issued Central Park forecast for this day."
    result.update(
        issued_at=current["issued"],
        source=current["model"],
        source_version=source_version(current),
        feature_f=current["value"],
        available_at=current["first_seen"],
        source_url=current["url"],
        sha256=current["digest"],
    )
    lead = event_end(day) - instant(current["issued"])
    decision_lead = event_end(day) - asof
    lead_band = int(lead.total_seconds() // (12 * 3600))
    result["lead_band_hours"] = [12 * lead_band, 12 * (lead_band + 1)]
    outcomes = db.execute(
        """SELECT event_day, MIN(high) high FROM venue_highs
        WHERE event_day<? AND first_seen<=? AND resolved<=? AND authority=?
        GROUP BY event_day HAVING MIN(high)=MAX(high)""",
        (day.isoformat(), stamp, stamp, AUTHORITY),
    )
    errors, climatology = [], []
    for outcome in outcomes:
        previous = date.fromisoformat(outcome["event_day"])
        if season_distance(day, previous) > SEASON_DAYS:
            continue
        actual = outcome["high"]
        climatology.append(actual)
        cut = event_end(previous) - decision_lead
        matched = db.execute(
            """SELECT issued,value,model,artifact FROM weather_records WHERE kind='forecast'
            AND event_day=? AND model=? AND first_seen<=? AND issued<=? AND published<=?
            ORDER BY issued DESC, first_seen DESC""",
            (previous.isoformat(), current["model"], iso(cut), iso(cut), iso(cut)),
        )
        for past in matched:
            past_lead = event_end(previous) - instant(past["issued"])
            if (
                source_version(past) == result["source_version"]
                and int(past_lead.total_seconds() // (12 * 3600)) == lead_band
                and (cut - instant(past["issued"])).total_seconds()
                <= FORECAST_TTL[current["model"]]
            ):
                errors.append(actual - past["value"])
                break
    if len(climatology) >= MIN_DAYS:
        result["baseline"] = sum(qualifies(x, bounds) for x in climatology) / len(
            climatology
        )
    count = len(errors)
    result["samples"] = count
    if count < MIN_DAYS:
        return (
            result,
            f"Calibration has {count} of {MIN_DAYS} earlier resolved station days in this lead and season window.",
        )
    yes = sum(qualifies(current["value"] + error, bounds) for error in errors)
    p = yes / count
    # Two-sided 95% Wilson interval for the empirical probability of this bin.
    z = 1.959963984540054
    denom = 1 + z**2 / count
    center = (p + z**2 / (2 * count)) / denom
    width = z / denom * math.sqrt(p * (1 - p) / count + z**2 / (4 * count**2))
    result.update(
        probability=p, lower=max(0.0, center - width), upper=min(1.0, center + width)
    )
    return result, None
