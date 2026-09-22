import PropTypes from 'prop-types';
import { EmptyState } from './ui/Bits.jsx';
import { formatINR } from './ui/format.js';

/**
 * The complaints behind the risk score.
 *
 * An officer needs to know *which* complaints drove a High rating, so this
 * table is the audit trail for the number. What it no longer leads with is the
 * arithmetic: "decay weight 0.8472" is an internal term from the scoring model
 * and means nothing to the person reading it. The weighting is still available
 * under "How the score is calculated" so the figure remains defensible.
 *
 * Rows open the full case, because the natural next question after "which
 * complaints?" is "show me that complaint".
 */
export default function ContributingCases({ contributions, totalScore, total, onOpenCase }) {
  if (!contributions || contributions.length === 0) {
    return (
      <EmptyState glyph="◔" title="No complaints linked yet">
        No reported cases currently trace to this destination, so nothing is driving the score.
      </EmptyState>
    );
  }

  const shown = contributions.length;
  const allOf = total || shown;
  const truncated = allOf > shown;

  return (
    <>
      <p className="section-lede">
        These are the complaints whose money reached this destination. A recent complaint counts
        for more than an old one.
      </p>

      <div className="table-wrap">
        <table className="table table-rows">
          <thead>
            <tr>
              <th>Case number</th>
              <th>Reported</th>
              <th>Age</th>
              <th className="num">Amount</th>
              <th>Weight in score</th>
            </tr>
          </thead>
          <tbody>
            {contributions.map((c) => (
              <tr
                key={c.case_id}
                className={onOpenCase ? 'is-clickable' : undefined}
                onClick={onOpenCase ? () => onOpenCase(c.case_id) : undefined}
                tabIndex={onOpenCase ? 0 : undefined}
                role={onOpenCase ? 'button' : undefined}
                onKeyDown={
                  onOpenCase
                    ? (e) => {
                        if (e.key === 'Enter' || e.key === ' ') {
                          e.preventDefault();
                          onOpenCase(c.case_id);
                        }
                      }
                    : undefined
                }
              >
                <td className="mono">{c.case_number}</td>
                <td>{(c.reported_at || '').slice(0, 10)}</td>
                <td>{c.age_days === 0 ? 'Today' : `${c.age_days} days ago`}</td>
                <td className="num">{formatINR(c.amount_inr) || '—'}</td>
                <td><WeightBar weight={Number(c.decay_weight) || 0} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {onOpenCase ? (
        <p className="hint">Select any case to open its full investigation.</p>
      ) : null}

      <details className="ranking-detail" style={{ marginTop: 'var(--sp-3)' }}>
        <summary>How the score is calculated</summary>
        <p className="ranking-lede">
          Each linked complaint adds 10 points. Those points halve as the complaint ages, so a
          case from last week counts for more than one from last year. The total is then placed on
          a 0&ndash;100 scale that rises steeply at first and flattens out, so that one person
          reporting the same wallet many times cannot dominate the result.
        </p>
        <table className="table ranking-table">
          <thead>
            <tr><th>Case</th><th className="num">Age (days)</th><th className="num">Weight</th><th className="num">Points</th></tr>
          </thead>
          <tbody>
            {contributions.map((c) => (
              <tr key={c.case_id}>
                <td className="mono">{c.case_number}</td>
                <td className="num">{c.age_days}</td>
                <td className="num mono">{Number(c.decay_weight).toFixed(4)}</td>
                <td className="num mono">{Number(c.points).toFixed(2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="ranking-lede">
          {truncated
            ? `The score of ${Number(totalScore).toFixed(1)} is computed across all ${allOf} linked
               cases. This table shows the ${shown} highest contributors, so the figures above are
               a subset rather than the whole calculation.`
            : `These figures produce the score of ${Number(totalScore).toFixed(1)} shown above, and
               can be checked by hand.`}
        </p>
      </details>
    </>
  );
}

/** Weight as a bar plus a word. The bar carries the comparison; the word stops
 *  it being a decimal nobody can interpret. */
function WeightBar({ weight }) {
  const pct = Math.max(2, Math.min(100, weight * 100));
  const word = weight >= 0.9 ? 'Full' : weight >= 0.6 ? 'High' : weight >= 0.3 ? 'Reduced' : 'Low';
  return (
    <span className="weight-cell">
      <span className="weight-track"><span className="weight-fill" style={{ width: `${pct}%` }} /></span>
      <span className="weight-word">{word}</span>
    </span>
  );
}
WeightBar.propTypes = { weight: PropTypes.number.isRequired };

ContributingCases.propTypes = {
  contributions: PropTypes.arrayOf(PropTypes.object),
  totalScore: PropTypes.number,
  total: PropTypes.number,
  onOpenCase: PropTypes.func,
};
ContributingCases.defaultProps = {
  contributions: [], totalScore: 0, total: 0, onOpenCase: undefined,
};
