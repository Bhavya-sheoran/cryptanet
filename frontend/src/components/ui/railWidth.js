/** Width of the navigation panel: bounds, default, and where a chosen width
 *  is remembered. Kept out of the component so the clamping can be tested. */

/** Below this the item labels truncate; above it the workspace suffers. */
export const RAIL_MIN = 168;
export const RAIL_MAX = 420;
export const RAIL_DEFAULT = 212;
export const RAIL_WIDTH_KEY = 'cryptanet.rail-width';

/**
 * Set the panel width, clamped, and return what was actually applied.
 *
 * The whole layout is a grid sized by `--rail-w`, so this one variable is all
 * a resize has to write. Clamping here rather than at the call sites means a
 * stored value from an older build, or a drag past the window edge, cannot
 * leave the panel unusably narrow or swallow the workspace.
 */
export function applyRailWidth(px) {
  const requested = Number(px);
  const width = Number.isFinite(requested)
    ? Math.min(RAIL_MAX, Math.max(RAIL_MIN, Math.round(requested)))
    : RAIL_DEFAULT;
  document.documentElement.style.setProperty('--rail-w', `${width}px`);
  return width;
}

/** The width in effect now, for a keyboard nudge to work from. */
export function currentRailWidth() {
  const value = parseInt(
    getComputedStyle(document.documentElement).getPropertyValue('--rail-w'), 10,
  );
  return Number.isFinite(value) ? value : RAIL_DEFAULT;
}
