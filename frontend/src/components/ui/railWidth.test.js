import { beforeEach, describe, expect, it } from 'vitest';
import {
  applyRailWidth, currentRailWidth, RAIL_DEFAULT, RAIL_MAX, RAIL_MIN,
} from './railWidth.js';

/**
 * The officer drags the panel edge, so the value arriving here is a raw
 * pointer position - including one from a drag that left the window. Clamping
 * is what stops that leaving the panel unusable.
 */
describe('rail width', () => {
  beforeEach(() => document.documentElement.style.removeProperty('--rail-w'));

  it('applies a width within the allowed range', () => {
    expect(applyRailWidth(260)).toBe(260);
    expect(document.documentElement.style.getPropertyValue('--rail-w')).toBe('260px');
  });

  it('refuses to shrink the panel past its labels or swallow the workspace', () => {
    expect(applyRailWidth(20)).toBe(RAIL_MIN);
    expect(applyRailWidth(9000)).toBe(RAIL_MAX);
  });

  it('falls back to the default rather than writing nonsense', () => {
    // A corrupted stored value must not produce "--rail-w: NaNpx".
    expect(applyRailWidth('not a width')).toBe(RAIL_DEFAULT);
    expect(document.documentElement.style.getPropertyValue('--rail-w')).toBe(`${RAIL_DEFAULT}px`);
  });

  it('reads back the width a keyboard nudge starts from', () => {
    applyRailWidth(300);
    expect(currentRailWidth()).toBe(300);
  });

  it('reports the default when nothing has been set yet', () => {
    expect(currentRailWidth()).toBe(RAIL_DEFAULT);
  });
});
