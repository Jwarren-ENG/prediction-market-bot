"""Recorded, cent-accurate valuations for the private portfolio chart."""

from datetime import timedelta

from .weather import instant, iso

WINDOWS = (15, 60, 240, 1440, 2880, 10080)
SCHEMA = "CREATE TABLE IF NOT EXISTS portfolio_marks(at TEXT PRIMARY KEY, equity INTEGER)"


def record(db, at, cents):
    """Keep at least one valuation per active minute, every change, and explicit gaps."""
    previous = db.execute("SELECT at,equity FROM portfolio_marks ORDER BY at DESC LIMIT 1").fetchone()
    if previous and (at < previous[0] or (at[:16] == previous[0][:16] and cents == previous[1])):
        return
    db.execute("INSERT OR REPLACE INTO portfolio_marks VALUES(?,?)", (at, cents))
    if not previous or previous[0][:10] != at[:10]:
        db.execute("DELETE FROM portfolio_marks WHERE at<?", (iso(instant(at) - timedelta(days=8)),))


def history(db, account, minutes, now, current, *, contract=False, gap_seconds=180):
    """Keep first, low, high and last in time order per chart bucket. Never bridge a gap."""
    if minutes not in WINDOWS:
        raise ValueError("Unsupported portfolio window.")
    table = "marks" if contract else "portfolio_marks"
    opening = now - timedelta(minutes=minutes)
    start_at, end_at = iso(opening), iso(now)
    first_at = db.execute(f"SELECT MIN(at) FROM {table}").fetchone()[0]
    rows = db.execute(f"SELECT at,equity FROM {table} WHERE at>=? AND at<? ORDER BY at", (start_at, end_at))
    start_ms, end_ms = round(opening.timestamp()*1000), round(now.timestamp()*1000)
    points = []
    segment = -1
    last_ms = None
    count = 0
    bucket = None
    band = []
    gaps = False

    def flush():
        if band:
            keep = {0, len(band)-1, min(range(len(band)), key=lambda i: band[i][1]),
                    max(range(len(band)), key=lambda i: band[i][1])}
            points.extend(band[i] for i in sorted(keep))
            band.clear()

    def observe(at_ms, cents):
        nonlocal last_ms, segment, bucket, count, gaps
        if cents is None:
            last_ms = None
            gaps = True
            return
        if last_ms is None or at_ms - last_ms > gap_seconds * 1000:
            gaps |= count > 0
            segment += 1
        slot = (at_ms - start_ms) * 144 // (minutes * 60000)
        if slot != bucket:
            flush()
            bucket = slot
        band.append([at_ms, cents, segment])
        last_ms = at_ms
        count += 1

    for at, cents in rows:
        observe(round(instant(at).timestamp()*1000), cents)
    observe(end_ms, current)
    flush()
    return dict(account=account, minutes=minutes, start_ms=start_ms, end_ms=end_ms,
                first_at=first_at or end_at, current_cents=current, observations=count,
                has_gaps=gaps, points=points)
