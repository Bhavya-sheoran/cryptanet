import PropTypes from 'prop-types';
import { RISK_TONE } from './ui/format.js';

const TONE_VAR = { ok: 'var(--ok-500)', warn: 'var(--warn-500)', danger: 'var(--dang-500)' };

/**
 * Risk score display.
 *
 * The number never appears alone. A ring shows magnitude at a glance, a
 * three-band scale shows where it falls against the configured thresholds, and
 * the contributing factors sit beside it — because "81" on its own tells an
 * investigator nothing about whether to act.
 */
export default function RiskPanel({ label, score, factors, halfLifeDays }) {
  const pct = Math.max(0, Math.min(100, Number(score) || 0));
  const tone = RISK_TONE[label] || 'neutral';
  const colour = TONE_VAR[tone] || 'var(--text-subtle)';

  const r = 46;
  const circumference = 2 * Math.PI * r;
  const filled = (pct / 100) * circumference;

  return (
    <div className="risk-hero">
      <div className="risk-gauge">
        <svg width="108" height="108" viewBox="0 0 108 108" aria-hidden="true">
          <circle cx="54" cy="54" r={r} fill="none" stroke="var(--bg-sunken)" strokeWidth="9" />
          <circle
            cx="54" cy="54" r={r} fill="none"
            stroke={colour} strokeWidth="9" strokeLinecap="round"
            strokeDasharray={`${filled} ${circumference - filled}`}
          />
        </svg>
        <div className="risk-gauge-label">
          <span className="risk-gauge-score" style={{ color: colour }}>{pct.toFixed(1)}</span>
          <span className="risk-gauge-of">/ 100</span>
        </div>
      </div>

      <div className="risk-meta grow">
        {/* The word leads, not the number. "High" is what an officer acts on;
            "81.4" is the arithmetic that produced it and means nothing without
            the scale, so it sits underneath as supporting detail. */}
        <span className={`badge badge-${tone} badge-uppercase risk-level-badge`}>
          {label} risk
        </span>
        <p className="risk-plain">
          How often money from reported frauds has arrived at this destination.
        </p>

        <div className="risk-scale" aria-hidden="true">
          <span className={`risk-scale-seg${pct > 0 ? ' on-low' : ''}`} />
          <span className={`risk-scale-seg${pct >= 40 ? ' on-medium' : ''}`} />
          <span className={`risk-scale-seg${pct >= 70 ? ' on-high' : ''}`} />
        </div>
        <div className="risk-scale-caption">
          <span>Low</span><span>Medium from 40</span><span>High from 70</span>
        </div>

        <ul className="factor-list" style={{ marginTop: 'var(--sp-4)' }}>
          {(factors || []).map((f) => <li key={f}>{f}</li>)}
        </ul>

        <p className="risk-note">
          The rating counts every reported case whose money reached here. Recent complaints count
          for more than old ones
          {halfLifeDays ? `, halving after ${halfLifeDays} days` : ''}, and the scale flattens
          out at the top so one person reporting the same wallet repeatedly cannot drive it
          to High on their own.
        </p>
      </div>
    </div>
  );
}

RiskPanel.propTypes = {
  label: PropTypes.string,
  score: PropTypes.number,
  factors: PropTypes.arrayOf(PropTypes.string),
  halfLifeDays: PropTypes.number,
};
RiskPanel.defaultProps = { label: 'unknown', score: 0, factors: [], halfLifeDays: null };
