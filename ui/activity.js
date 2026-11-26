import {count, day, esc, price, stamp} from './common.js';
import {quoteRows, quoteStatus} from './markets.js';

const statusLabels = {
  live:'Receiving Kalshi quotes',empty:'No open contracts',waiting:'Waiting for the first quote',
  disabled:'Market capture is off',offline:'Worker offline',disconnected:'Worker disconnected',
  stale:'Latest prices delayed',error:'Latest quote request failed',rate_limited:'Waiting for Kalshi’s rate limit',unavailable:'Quote feed unavailable',
};

export function latestPriceMoves(markets, interval, limit = 8) {
  const moves = [];
  for (const m of markets) {
    const firstPoll = m.history[0]?.at ?? Infinity;
    for (const [points,label,maxGap] of [[m.history,'Live poll',interval*2000],[m.candles,'One-minute close',60000]]) {
      let found = 0;
      for (let i=points.length-1; i>0 && found<limit; --i) {
        const now = points[i], prev = points[i-1];
        if (label === 'One-minute close' && now.at >= firstPoll) continue;
        if (now.at-prev.at > maxGap || now.at<=prev.at) continue;
        const changes = [];
        for (const side of ['bid','ask']) {
          if (prev[side] !== null && now[side] !== null && prev[side] !== now[side])
            changes.push(`${side === 'bid' ? 'Bid' : 'Ask'} ${price(prev[side])} → ${price(now[side])}`);
        }
        if (!changes.length) continue;
        moves.push({at:now.at, id:m.id, label:m.label || m.id, eventDate:m.event_date, source:label, detail:changes.join(' · ')});
        found++;
      }
    }
  }
  return moves.sort((a,b)=>b.at-a.at || a.id.localeCompare(b.id)).slice(0,limit);
}

export function marketActivity(state,ui) {
  if (state.mode !== 'forward') return '';
  const feed = state.quotes || {};
  const status = quoteStatus(state,ui.connected);
  const quoteList = quoteRows(state,ui.connected);
  const comparable = quoteList.filter(row => row.bidChange !== null || row.askChange !== null);
  const changed = comparable.filter(row => row.bidChange || row.askChange).length;
  const interval = feed.interval_seconds || 15;
  let pollSummary = '';
  if (status === 'live') {
    pollSummary = comparable.length ?
      `${count(changed)} of ${count(comparable.length)} comparable contracts changed price at the latest poll.` :
      'Waiting for consecutive comparable quotes.';
  }
  const moves = latestPriceMoves(quoteList,interval);
  const active = status === 'live' || status === 'empty';
  return `<section class="panel activity-market" aria-labelledby="market-activity-title"><div class="panel-head"><h2 id="market-activity-title">Market updates</h2>
    <span class="connection-status"><i class="state-symbol ${active ? 'connected' : 'offline'}" aria-hidden="true"></i>${esc(statusLabels[status] || 'Waiting for market data')}</span></div>
    <div class="activity-facts"><div><span>Last venue response</span><strong data-testid="activity-quote-time">${esc(stamp(feed.last_success_at))}</strong></div>
    <div><span>Last quote check</span><strong>${esc(stamp(feed.last_checked_at))}</strong></div>
    <div><span>Watchlist</span><strong>${count(quoteList.length)} contracts</strong></div></div>
    ${pollSummary ? `<p class="activity-caption">${esc(pollSummary)}</p>` : ''}
    ${moves.length ? `<div class="activity-changes"><h3>Recent price moves</h3><div class="table-scroll" tabindex="0" role="region" aria-label="Recent YES price movements"><table class="data-table">
      <thead><tr><th scope="col">When</th><th scope="col">Contract</th><th scope="col">YES price change</th><th scope="col">Source</th></tr></thead>
      <tbody>${moves.map(m=>`<tr data-key="move-${esc(m.id)}-${m.at}"><td class="time-cell numeric">${esc(stamp(m.at))}</td><th scope="row">${esc(m.label)}<div class="row-meta">${esc(day(m.eventDate))}</div></th>
        <td class="numeric">${esc(m.detail)}</td><td>${esc(m.source)}</td></tr>`).join('')}</tbody></table></div></div>` :
      active ? '<p class="activity-caption">A price move appears here when consecutive observations show a different bid or ask.</p>' : ''}</section>`;
}
