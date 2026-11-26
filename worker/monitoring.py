"""Recorded activity windows and aligned forward-paper portfolio marks."""

from datetime import datetime, timezone


WINDOWS = {24: 3600, 48: 7200, 168: 21600}


def stamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000000Z"
    )


def counts(db, series, start, end, stride):
    # Both storage formats write UTC timestamps. Match whole seconds explicitly
    # so a row at xx:59.999999 cannot round into the next SQLite time bucket.
    table, column, condition = {
        "decisions": ("activity", "at", "kind='decision'"),
        "scans": ("forward_scans", "finished_at", "finished_at IS NOT NULL"),
    }[series]
    first = db.execute(
        f"SELECT min({column}) FROM {table} WHERE {condition}"
    ).fetchone()[0]
    sql = f"""SELECT (strftime('%s',substr({column},1,19)) - ?) / ? AS slot, count(*)
              FROM {table} WHERE {condition} AND {column} >= ? AND {column} < ?
              GROUP BY slot"""
    bins = [0] * ((end - start) // stride)
    for slot, total in db.execute(sql, (start, stride, stamp(start), stamp(end))):
        if slot is not None and 0 <= slot < len(bins):
            bins[slot] = total
    return {"first_at": first, "counts": bins}


PAIR_SCHEMA = """
CREATE TABLE IF NOT EXISTS paired_equity(
    bucket INTEGER NOT NULL, opening INTEGER NOT NULL, at TEXT NOT NULL,
    weather INTEGER NOT NULL, bitcoin INTEGER NOT NULL, hold INTEGER NOT NULL,
    weather_peak INTEGER NOT NULL, bitcoin_peak INTEGER NOT NULL, hold_peak INTEGER NOT NULL,
    weather_dd INTEGER NOT NULL, bitcoin_dd INTEGER NOT NULL, hold_dd INTEGER NOT NULL,
    PRIMARY KEY(bucket,opening));
"""
ACCOUNTS = ("weather", "bitcoin", "hold")


def comparable_weather(mark):
    return bool(
        mark
        and all(p["mark_status"] == "executable" for p in mark["positions"])
        and all(order["status"] != "unknown" for order in mark["orders"])
    )


def record_pair(db, at, weather_cents, btc_cents, hold_cents, *, opening=False):
    amounts = (weather_cents, btc_cents, hold_cents)
    if any(type(v) is not int or v < 0 for v in amounts):
        return False
    bucket = int(datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp()) // 300
    # Serialize the peak calculation and the insert across concurrent readers.
    db.execute("BEGIN IMMEDIATE")
    previous = db.execute(
        "SELECT * FROM paired_equity ORDER BY bucket DESC,opening LIMIT 1"
    ).fetchone()
    if previous and (
        opening
        or bucket < previous["bucket"]
        or (bucket == previous["bucket"] and not previous["opening"])
    ):
        return False
    peaks = [
        max(value, previous[name + "_peak"]) if previous else value
        for name, value in zip(ACCOUNTS, amounts)
    ]
    drops = [
        max(previous[name + "_dd"], peak - value) if previous else 0
        for name, peak, value in zip(ACCOUNTS, peaks, amounts)
    ]
    db.execute(
        "INSERT INTO paired_equity VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (bucket, int(opening), at, *amounts, *peaks, *drops),
    )
    return True


def comparison(db):
    if db.in_transaction:
        return _comparison(db)
    db.execute("BEGIN")
    try:
        return _comparison(db)
    finally:
        db.rollback()


def _comparison(db):
    first = db.execute(
        "SELECT * FROM paired_equity ORDER BY bucket,opening DESC LIMIT 1"
    ).fetchone()
    if first is None:
        return {"start_at": None, "as_of": None, "points": [], "accounts": {}}
    last = db.execute(
        "SELECT * FROM paired_equity ORDER BY bucket DESC,opening LIMIT 1"
    ).fetchone()
    accounts = {}
    for name in ACCOUNTS:
        start, end = first[name], last[name]
        accounts[name] = {
            "start_cents": start,
            "end_cents": end,
            "net_cents": end - start,
            "return_pct": 100 * (end - start) / start if start else None,
            "drawdown_cents": last[name + "_dd"],
        }
    # Keep the original baseline, recent hourly marks and older daily marks.
    rows = db.execute(
        """SELECT at,weather AS weather_cents,bitcoin AS btc_cents,hold AS hold_cents
                         FROM paired_equity WHERE bucket IN (
                             SELECT min(bucket) FROM paired_equity UNION
                             SELECT max(bucket) FROM paired_equity WHERE bucket < ? GROUP BY bucket/288 UNION
                             SELECT max(bucket) FROM paired_equity WHERE bucket >= ? GROUP BY bucket/12)
                         ORDER BY bucket,opening DESC""",
        (last["bucket"] - 2016, last["bucket"] - 2016),
    )
    return {
        "start_at": first["at"],
        "as_of": last["at"],
        "accounts": accounts,
        "points": [dict(row) for row in rows],
    }
