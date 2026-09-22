import { describe, expect, it } from 'vitest';
import {
  activityPattern, countOf, formatDuration, formatINR, truncateMiddle,
} from './format.js';

/**
 * These helpers are the whole plain-language layer. An officer with no
 * blockchain background reads their output and nothing else, so the cases
 * below are written against the actual values the API returns for the demo
 * dataset rather than round numbers chosen to make the assertions easy.
 */

describe('formatINR', () => {
  it('renders the Meridian exposure figure in lakh', () => {
    // total_volume_inr as returned by GET /api/v1/exposure for the TRON
    // demo wallet. "₹20.04 lakh" is read at a glance; the raw figure is not.
    expect(formatINR(2003813.202072)).toBe('₹20.04 lakh');
  });

  it('switches to crore above a hundred lakh', () => {
    expect(formatINR(35000000)).toBe('₹3.5 crore');
  });

  it('leaves sub-lakh amounts as grouped rupees rather than forcing "0.4 lakh"', () => {
    expect(formatINR(45000)).toBe('₹45,000');
  });

  it('drops trailing zeros so whole amounts read cleanly', () => {
    expect(formatINR(2000000)).toBe('₹20 lakh');
  });

  it('keeps the sign on a negative amount', () => {
    expect(formatINR(-2003813.202072)).toBe('-₹20.04 lakh');
  });

  it('returns null when there is no figure, so callers can fall back', () => {
    // The caller shows the raw asset amount instead. Returning null rather
    // than "₹0" matters: "₹0" is a claim, absence is not.
    expect(formatINR(null)).toBeNull();
    expect(formatINR(undefined)).toBeNull();
    expect(formatINR('not a number')).toBeNull();
  });
});

describe('formatDuration', () => {
  it('renders the demo recency as days and hours', () => {
    // seconds_since_last from the live payload.
    expect(formatDuration(769935.184504, { suffix: 'ago' })).toBe('8 days 21 hours ago');
  });

  it('renders the demo inter-hop gap as hours and minutes', () => {
    // median_inter_hop_seconds = 7800 -> the gap shown on the route.
    expect(formatDuration(7800)).toBe('2 hours 10 minutes');
  });

  it('never shows more than two units', () => {
    const out = formatDuration(86400 + 3600 + 61);
    expect(out).toBe('1 day 1 hour');
    expect(out).not.toMatch(/minute/);
  });

  it('singularises', () => {
    expect(formatDuration(1)).toBe('1 second');
    expect(formatDuration(60)).toBe('1 minute');
    expect(formatDuration(3600)).toBe('1 hour');
    expect(formatDuration(86400)).toBe('1 day');
  });

  it('omits the smaller unit when it is zero', () => {
    expect(formatDuration(7200)).toBe('2 hours');
    expect(formatDuration(172800)).toBe('2 days');
  });

  it('clamps a negative duration rather than saying "-3 seconds ago"', () => {
    // Clock skew between the node and the API should not produce nonsense.
    expect(formatDuration(-3)).toBe('0 seconds');
  });

  it('returns null for a missing value', () => {
    expect(formatDuration(null)).toBeNull();
    expect(formatDuration(undefined)).toBeNull();
  });
});

describe('countOf', () => {
  it('pluralises regular nouns', () => {
    expect(countOf(7, 'hop')).toBe('7 hops');
    expect(countOf(1, 'hop')).toBe('1 hop');
    expect(countOf(0, 'hop')).toBe('0 hops');
  });

  it('takes an explicit plural where the regular one would be wrong', () => {
    expect(countOf(2, 'party', 'parties')).toBe('2 parties');
  });

  it('groups large counts in the Indian system', () => {
    expect(countOf(100000, 'transfer')).toBe('1,00,000 transfers');
  });

  it('treats a missing count as zero rather than printing NaN', () => {
    expect(countOf(null, 'hop')).toBe('0 hops');
    expect(countOf(undefined, 'transfer')).toBe('0 transfers');
  });
});

describe('activityPattern', () => {
  it('warns when the hops run backwards in time', () => {
    // continuity_ok === false is the one case that changes an officer's
    // reading of the path, so it must be worded as an observation and
    // toned as a warning.
    const p = activityPattern({ continuity_ok: false, transfer_count: 4 });
    expect(p.label).toBe('Inconsistent timing');
    expect(p.tone).toBe('warn');
    expect(p.detail).toMatch(/out of order/i);
  });

  it('reports repeated transfers when money arrived more than once', () => {
    const p = activityPattern({ continuity_ok: true, transfer_count: 4 });
    expect(p.label).toBe('Repeated transfers');
    expect(p.detail).toMatch(/4 separate times/);
  });

  it('reports a single transfer for the demo wallet', () => {
    // transfer_count = 1 in the live payload.
    const p = activityPattern({ continuity_ok: true, transfer_count: 1 });
    expect(p.label).toBe('Single transfer');
    expect(p.tone).toBe('ok');
  });

  it('never names the internal flag', () => {
    const wordings = [
      activityPattern({ continuity_ok: false, transfer_count: 1 }),
      activityPattern({ continuity_ok: true, transfer_count: 1 }),
      activityPattern({ continuity_ok: true, transfer_count: 3 }),
    ];
    wordings.forEach((p) => {
      expect(`${p.label} ${p.detail}`).not.toMatch(/continuity/i);
    });
  });

  it('degrades to Unknown rather than throwing when features are absent', () => {
    expect(activityPattern(null).label).toBe('Unknown');
  });
});

describe('truncateMiddle', () => {
  it('keeps both ends of an address, which is what gets compared', () => {
    const addr = 'TFxXYrP93fNJjoFKa2EaVU8UM3FqabDJ8a';
    expect(truncateMiddle(addr, 10, 8)).toBe('TFxXYrP93f…FqabDJ8a');
  });

  it('leaves a short value alone rather than adding an ellipsis', () => {
    expect(truncateMiddle('abc', 10, 6)).toBe('abc');
  });

  it('returns an empty string for a missing value', () => {
    expect(truncateMiddle(null)).toBe('');
  });
});
