/** Presentation helpers shared across components. Non-component exports live
 *  here so component modules stay Fast-Refresh friendly. */

/** Risk label -> badge tone. One mapping used everywhere, so "high" is never
 *  amber in one table and red in another. */
export const RISK_TONE = { low: 'ok', medium: 'warn', high: 'danger' };

/** Head/tail truncation for hashes and addresses, keeping both ends legible -
 *  the ends are what an investigator actually compares. */
export function truncateMiddle(value, head = 10, tail = 6) {
  if (!value) return '';
  if (value.length <= head + tail + 1) return value;
  return `${value.slice(0, head)}…${value.slice(-tail)}`;
}

export function relativeTime(iso) {
  if (!iso) return null;
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return null;
  const secs = Math.max(0, (Date.now() - then.getTime()) / 1000);
  if (secs < 60) return `${Math.floor(secs)}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

/** Rupees in the units an Indian investigator reads without counting digits.
 *
 *  "₹20.04 lakh" is understood at a glance; "₹2,003,813" has to be counted, and
 *  "0.1981" means nothing at all. Below a lakh the grouped figure is already
 *  readable, so it is left alone rather than forced into an awkward "0.4 lakh".
 */
export function formatINR(value) {
  if (value == null || Number.isNaN(Number(value))) return null;
  const n = Number(value);
  const sign = n < 0 ? '-' : '';
  const abs = Math.abs(n);

  if (abs >= 10000000) return `${sign}₹${trimZeros(abs / 10000000)} crore`;
  if (abs >= 100000) return `${sign}₹${trimZeros(abs / 100000)} lakh`;
  return `${sign}₹${Math.round(abs).toLocaleString('en-IN')}`;
}

/** Two decimals at most, and none when the value is whole: "₹20 lakh" reads
 *  better than "₹20.00 lakh", while "₹20.04 lakh" keeps a figure that matters. */
function trimZeros(n) {
  return Number(n.toFixed(2)).toLocaleString('en-IN');
}

/** A duration in seconds as plain English.
 *
 *  "8 days 21 hours ago" instead of "768519 seconds" or "213 hours". Two units
 *  at most - "8 days 21 hours 28 minutes" is precision nobody asked for.
 */
export function formatDuration(seconds, { suffix = '' } = {}) {
  if (seconds == null || Number.isNaN(Number(seconds))) return null;
  const total = Math.max(0, Math.floor(Number(seconds)));
  const tail = suffix ? ` ${suffix}` : '';

  if (total < 60) return `${total} second${total === 1 ? '' : 's'}${tail}`;

  const minutes = Math.floor(total / 60);
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? '' : 's'}${tail}`;

  const hours = Math.floor(total / 3600);
  if (hours < 24) {
    const mins = Math.floor((total % 3600) / 60);
    const head = `${hours} hour${hours === 1 ? '' : 's'}`;
    return mins ? `${head} ${mins} minute${mins === 1 ? '' : 's'}${tail}` : `${head}${tail}`;
  }

  const days = Math.floor(total / 86400);
  const hrs = Math.floor((total % 86400) / 3600);
  const head = `${days} day${days === 1 ? '' : 's'}`;
  return hrs ? `${head} ${hrs} hour${hrs === 1 ? '' : 's'}${tail}` : `${head}${tail}`;
}

/** A whole-number count with its noun, pluralised. */
export function countOf(n, singular, plural) {
  const value = Number(n) || 0;
  return `${value.toLocaleString('en-IN')} ${value === 1 ? singular : plural || `${singular}s`}`;
}

/** Plain-English reading of the timeline-consistency flag.
 *
 *  `continuity_ok` is true when each hop happened after the one before it.
 *  False means the path runs backwards in time, which usually means the hops
 *  are not actually a single chain of payments. The wording says what was
 *  observed rather than naming the internal flag.
 */
export function activityPattern(features) {
  if (!features) return { label: 'Unknown', detail: null, tone: 'neutral' };

  if (features.continuity_ok === false) {
    return {
      label: 'Inconsistent timing',
      detail: 'Some transfers appear out of order in time, so these may not be one chain of payments.',
      tone: 'warn',
    };
  }

  const transfers = Number(features.transfer_count) || 0;
  if (transfers > 1) {
    return {
      label: 'Repeated transfers',
      detail: `Money reached this destination ${transfers} separate times, in order.`,
      tone: 'ok',
    };
  }
  return {
    label: 'Single transfer',
    detail: 'Money reached this destination once, and the timing is consistent.',
    tone: 'ok',
  };
}
