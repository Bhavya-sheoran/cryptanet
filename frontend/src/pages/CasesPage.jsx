import { useCallback, useEffect, useState } from 'react';
import PropTypes from 'prop-types';
import { fetchCases } from '../api/client.js';
import { Badge, EmptyState, Skeleton, TimeAgo } from '../components/ui/Bits.jsx';
import { formatINR } from '../components/ui/format.js';

const SOURCE_TONE = { ncrp_mock: 'info', synthetic: 'neutral', manual: 'neutral' };
const STATUS_TONE = { open: 'warn', tracing: 'info', analysed: 'ok', escalated: 'danger', closed: 'neutral' };
const STATUS_WORD = {
  open: 'Open', tracing: 'Tracing', analysed: 'Analysed', escalated: 'Escalated', closed: 'Closed',
};
const SOURCE_WORD = {
  ncrp_mock: 'NCRP feed', manual: 'Filed here', synthetic: 'Filed here',
};

/**
 * Case list, and the case file for whichever case is selected.
 *
 * Master/detail rather than navigation: an investigator comparing cases wants
 * the list to stay put while they read one.
 */
export default function CasesPage({ currentUser, onOpenCase }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    if (!currentUser) return;
    try {
      setData(await fetchCases(50));
      setError(null);
    } catch (err) {
      setError(err.message);
      setData({ total: 0, cases: [] });
    }
  }, [currentUser]);

  useEffect(() => { load(); }, [load]);

  if (!currentUser) {
    return (
      <section className="panel">
        <div className="panel-body">
          <EmptyState glyph="▤" title="Sign in to view cases">
            Case records, notes and evidence are only available to a signed-in officer.
          </EmptyState>
        </div>
      </section>
    );
  }

  return (
    <>
      <div className="page-head">
        <div>
          <div className="crumb">Workspace</div>
          <h1>Cases</h1>
        </div>
        <button type="button" className="btn btn-secondary" onClick={load}>Refresh</button>
      </div>

      {error ? <div className="callout callout-danger"><span className="glyph">⚠</span><div>{error}</div></div> : null}

      <section className="panel">
        <div className="panel-head">
          <h2>All cases {data ? <span className="badge badge-neutral">{data.total}</span> : null}</h2>
        </div>
          <div className="panel-body flush">
            {data === null ? (
              <div className="panel-body stack">
                <Skeleton height="16px" /><Skeleton height="16px" /><Skeleton height="16px" />
              </div>
            ) : data.cases.length === 0 ? (
              <EmptyState glyph="▤" title="No cases filed">
                Report a suspect wallet from the Investigate tab to open the first case.
              </EmptyState>
            ) : (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Case</th>
                      <th>Status</th>
                      <th>Source</th>
                      <th className="num">Amount (INR)</th>
                      <th>Reported</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.cases.map((c) => (
                      <tr
                        key={c.case_id}
                        className="is-clickable"
                        onClick={() => onOpenCase?.(c.case_id)}
                        tabIndex={0}
                        role="button"
                        onKeyDown={(e) => {
                          if (e.key === 'Enter' || e.key === ' ') {
                            e.preventDefault();
                            onOpenCase?.(c.case_id);
                          }
                        }}
                      >
                        <td className="mono">{c.case_number}</td>
                        <td><Badge tone={STATUS_TONE[c.status] || 'neutral'}>{STATUS_WORD[c.status] || c.status}</Badge></td>
                        <td>
                          <Badge tone={SOURCE_TONE[c.source] || 'neutral'}>
                            {SOURCE_WORD[c.source] || c.source}
                          </Badge>
                        </td>
                        <td className="num">{formatINR(c.amount_inr) || '—'}</td>
                        <td><TimeAgo iso={c.reported_at} /></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
        </div>
        <div className="panel-foot">Select any case to open its full investigation.</div>
      </section>
    </>
  );
}

CasesPage.propTypes = {
  currentUser: PropTypes.shape({ role: PropTypes.string, can_approve: PropTypes.bool }),
  onOpenCase: PropTypes.func,
};
CasesPage.defaultProps = { currentUser: null, onOpenCase: undefined };
