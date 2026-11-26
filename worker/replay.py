"""Deterministic, offline path from synthetic source records to a paper settlement."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

from . import weather
from .ledger import Ledger

FIXTURE_PATH = Path(__file__).with_name("fixtures") / "central_park_replay.json"


def _history(db, training):
    """Expand the versioned fixture into distinct, previously resolved station days."""
    forecasts, errors = training["forecast_cycle_f"], training["error_cycle_f"]
    count = 0
    for year in range(training["first_year"], training["last_year"] + 1):
        start = date(year, training["first_month"], training["first_day"])
        for index in range(training["days_per_year"]):
            day = start + timedelta(days=index)
            end = weather.event_end(day)
            issued = end - timedelta(hours=17)
            prediction = forecasts[count % len(forecasts)]
            high = prediction + errors[count % len(errors)]
            url = "fixture://central-park/history/" + day.isoformat()
            weather.save(
                db,
                "forecast",
                day,
                weather.POINT_MODEL,
                issued,
                issued + timedelta(minutes=1),
                url,
                prediction,
                "Synthetic issued history.",
                {"high_f": prediction},
            )
            resolved = weather.iso(end + timedelta(hours=8))
            db.execute(
                "INSERT INTO venue_highs VALUES(?,?,?,?,?,?)",
                (day.isoformat(), weather.AUTHORITY, high, resolved, resolved, url),
            )
            count += 1


def _point(db, spec):
    if "issued_at" not in spec:
        return
    issued = weather.instant(spec["issued_at"])
    day = date.fromisoformat(spec["event_date"])
    start = weather.midnight(day) + timedelta(hours=7)
    product = {
        "properties": {
            "updateTime": spec["issued_at"],
            "generatedAt": spec["issued_at"],
            "periods": [
                {
                    "isDaytime": True,
                    "temperatureUnit": "F",
                    "temperature": spec["point_f"],
                    "startTime": weather.iso(start),
                    "endTime": weather.iso(start + timedelta(hours=10)),
                }
            ],
        }
    }
    weather.read_point(
        db,
        product,
        "fixture://central-park/issued/" + spec["event_date"],
        issued + timedelta(seconds=5),
    )


def _bulletin(db, spec, completed):
    issue = weather.instant(
        spec["complete_at"] if completed else spec["preliminary_at"]
    )
    day = date.fromisoformat(spec["event_date"])
    months = "JANUARY FEBRUARY MARCH APRIL MAY JUNE JULY AUGUST SEPTEMBER OCTOBER NOVEMBER DECEMBER".split()
    when = f"{months[day.month - 1]} {day.day:02d} {day.year}"
    coverage = "YESTERDAY" if completed else "TODAY"
    # The parser expects English month names. Persist the synthetic raw bulletin
    # and build timeline evidence through the same adapter used by forward scans.
    body = (
        f"CLINYC\nCENTRAL PARK NY CLIMATE SUMMARY FOR {when}\n"
        + ("" if completed else "VALID TODAY AS OF 0400 PM LOCAL TIME\n")
        + f"TEMPERATURE (F)\n {coverage}\n MAXIMUM {spec['high_f']}\n"
    )
    data = {
        "productText": body,
        "issuanceTime": weather.iso(issue),
        "issuingOffice": "KOKX",
        "productCode": "CLI",
    }
    suffix = "completed" if completed else "preliminary"
    weather.read_cli(
        db,
        data,
        "fixture://central-park/bulletin/" + suffix,
        issue + timedelta(seconds=5),
    )


def _packet(db, spec, at, levels):
    day = date.fromisoformat(spec["event_date"])
    instant = weather.instant(at)
    low, high = spec["bounds"]
    label = f"{low} to {high}°F"
    forecast, gap = weather.estimate(db, day, (low, high), instant)
    evidence = weather.records(db, instant, day)
    for record in evidence:
        record["title"] = "Synthetic " + record["title"].lower()
    rules = (
        f"YES pays $1 if the final daily maximum at Central Park (KNYC) is {low} through {high}°F, "
        f"inclusive, for {day:%B %d, %Y} in Eastern local time. Use the completed CLINYC report. "
        "The synthetic venue publishes the final determination and may delay it for corrections. "
        "A documented void returns purchase premium and retains entry fees."
    )
    rule_day = (day - timedelta(days=2)).isoformat() + "T00:00:00Z"
    evidence.insert(
        0,
        weather.evidence(
            "Synthetic contract terms",
            "fixture://venue/rules/" + spec["id"],
            rule_day,
            rule_day,
            "Inclusive temperature range, named station and final venue authority.",
            kind="rules",
        ),
    )
    return {
        "id": spec["id"],
        "event_id": spec["event_id"],
        "title": f"Will Central Park reach {label}?",
        "label": label,
        "station": "KNYC · Central Park",
        "event_date": spec["event_date"],
        "close_at": weather.iso(weather.event_end(day)),
        "as_of": at,
        "observed_at": at,
        "book_source": "fixture",
        "levels": levels,
        "forecast": forecast,
        "rules": rules,
        "rule_version": "synthetic-knyc-v1",
        "evidence": evidence,
        "fee_rate": "0.07",
        "fee_model": "quadratic",
        "tradable": True,
        "missing": [gap] if gap else [],
        "bounds": [low, high],
        "unit": "°F",
        "timezone": "America/New_York",
        "void_policy": "refund_premium_keep_fees",
        "synthetic": True,
    }


def replay_demo(ledger: Ledger):
    """Replay is safe to repeat after a restart. Every historical receipt has a stable ID."""
    spec = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    case = spec["settled"]
    with closing(sqlite3.connect(":memory:")) as db:
        db.row_factory = sqlite3.Row
        weather.initialize(db)
        _history(db, spec["training"])
        _point(db, case)
        _bulletin(db, case, completed=False)
        first = _packet(db, case, case["first_at"], case["first_levels"])
        second = _packet(db, case, case["second_at"], case["second_levels"])
        ledger.ingest([first], case["first_at"])
        ledger.ingest([second], case["second_at"])
        _bulletin(db, case, completed=True)
        complete = weather.iso(
            weather.instant(case["complete_at"]) + timedelta(seconds=5)
        )
        final_evidence = weather.records(
            db, weather.instant(complete), case["event_date"]
        )
        for item in final_evidence:
            item["title"] = "Synthetic " + item["title"].lower()
        update = {
            **second,
            "as_of": complete,
            "tradable": False,
            "evidence": [second["evidence"][0], *final_evidence],
        }
        ledger.ingest([update], complete)
        ledger.settle(
            case["id"], case["venue_result"], case["final_at"], "fixture:venue-final"
        )
        for m in spec["inbox"]:
            _point(db, m)
        live = [_packet(db, m, spec["inbox_at"], m["levels"]) for m in spec["inbox"]]
        ledger.ingest(live, spec["inbox_at"])

    state = ledger.state()
    # Timeline outcomes are read from durable decisions, including on a second run.
    decisions = [
        e
        for e in reversed(state["activity"])
        if e["kind"] == "decision" and e["market_id"] == case["id"]
    ]
    comparison = [
        {
            k: e.get(k)
            for k in ("decision", "at", "side", "qty", "avg_ask_cents", "net_cents")
        }
        for e in decisions
        if e["decision"] in ("Pass", "Paper buy")
    ]
    portfolio = state["portfolio"]
    return {
        "id": spec["id"],
        "case_id": case["id"],
        "as_of": spec["inbox_at"],
        "synthetic": True,
        "comparison": comparison,
        "forecast_probability": first["forecast"]["probability"],
        "forecast_lower": first["forecast"]["lower"],
        "calibration_days": first["forecast"]["samples"],
        "cash_cents": portfolio["cash_cents"],
        "realized_cents": portfolio["realized_cents"],
        "fees_cents": portfolio["fees_cents"],
        "reserved_cents": portfolio["reserved_cents"],
        "open_positions": len(portfolio["positions"]),
        "result": case["venue_result"],
    }
