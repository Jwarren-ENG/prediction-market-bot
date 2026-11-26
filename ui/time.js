const eastern = 'America/New_York';
const storageKey = 'pmb.displayTimeZone';
const presets = new Map([
  [eastern, 'Eastern · New York'], ['America/Chicago', 'Central · Chicago'],
  ['America/Denver', 'Mountain · Denver'], ['America/Los_Angeles', 'Pacific · Los Angeles'],
  ['America/Anchorage', 'Alaska · Anchorage'], ['Pacific/Honolulu', 'Hawaii · Honolulu'],
  ['UTC', 'UTC'], ['device', 'Use this device’s time zone'],
]);
const places = typeof Intl.supportedValuesOf === 'function' ? Intl.supportedValuesOf('timeZone') : ['Europe/London','Europe/Paris','Asia/Kolkata','Asia/Tokyo','Australia/Sydney'];
export const timeZoneChoices = [...presets, ...places.filter(id => !presets.has(id)).map(id => [id,id.replaceAll('_',' ')])];
const dateOnly = {
  long: new Intl.DateTimeFormat('en-US',{timeZone:'UTC',month:'short',day:'numeric',year:'numeric'}),
  short: new Intl.DateTimeFormat('en-US',{timeZone:'UTC',month:'short',day:'numeric'}),
};
let selection, formats;
export function instant(value) {
  if (value === null || value === undefined || value === '' || (typeof value !== 'string' && typeof value !== 'number' && !(value instanceof Date))) return null;
  const date = value instanceof Date ? value : new Date(value);
  return Number.isFinite(date.getTime()) ? date : null;
}
function useZone(choice) {
  if (!timeZoneChoices.some(([id]) => id === choice)) return false;
  const timeZone = choice === 'device' ? Intl.DateTimeFormat().resolvedOptions().timeZone : choice;
  const formatter = (locale, options) => new Intl.DateTimeFormat(locale,{timeZone,...options});
  formats = {
    long: formatter('en-US',{month:'short',day:'numeric',year:'numeric'}),
    short: formatter('en-US',{month:'short',day:'numeric'}),
    time: formatter('en-GB',{hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}),
    wall: formatter('en-US',{hour:'numeric',minute:'2-digit',second:'2-digit',timeZoneName:'short'}),
    zone: formatter('en-US',{hour:'numeric',timeZoneName:'short'}),
  };
  selection = choice;
  return true;
}
export function setDisplayTimeZone(choice) {
  if (!useZone(choice)) return false;
  try { if (typeof window !== 'undefined') window.localStorage.setItem(storageKey,choice); } catch {}
  return true;
}
export function reloadDisplayTimeZone() {
  let choice;
  try { if (typeof window !== 'undefined') choice = window.localStorage.getItem(storageKey); } catch {}
  if (!useZone(choice)) useZone(eastern);
}
reloadDisplayTimeZone();
export const displayTimeZone = () => selection;
export const wallClock = (value = Date.now()) => formats.wall.format(value);
export function zoneLabel(value = Date.now()) {
  const date = instant(value) || new Date();
  return formats.zone.formatToParts(date).find(part => part.type === 'timeZoneName')?.value || formats.zone.resolvedOptions().timeZone;
}
export function day(value,short = false) {
  const date = instant(value);
  if (!date) return '—';
  // Contracts carry a civil event date. Changing the clock must never shift the event to yesterday.
  const group = typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) ? dateOnly : formats;
  return group[short ? 'short' : 'long'].format(date);
}
export function clock(value) {
  const date = instant(value);
  return date ? formats.time.format(date) : '—';
}
export const stamp = value => instant(value) ? `${day(value)} · ${clock(value)} ${zoneLabel(value)}` : '—';
