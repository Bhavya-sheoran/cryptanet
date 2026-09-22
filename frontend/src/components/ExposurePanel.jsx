import { useCallback, useEffect, useState } from 'react';
import PropTypes from 'prop-types';
import AddressChip from './ui/AddressChip.jsx';
import SankeyTrace from './SankeyTrace.jsx';
import { Badge, EmptyState, Skeleton, TimeAgo } from './ui/Bits.jsx';
import { activityPattern, countOf, formatDuration, formatINR } from './ui/format.js';
import { fetchExposure } from '../api/client.js';

const SERVICE_TONE = {
  exchange: 'ok',
  mixer: 'warn',
  sanctioned: 'danger',
  darknet: 'danger',
  gambling: 'warn',
  payment_processor: 'neutral',
};

/**
 * Where the money ended up.
 *
 * Written for an investigating officer with no blockchain background, so the
 * six weighted features behind the ranking are shown as what they measure -
 * an amount in rupees, a number of hops, a time since the last movement -
 * rather than as the normalised contributions the scorer works in. A figure
 * like 0.1981 is meaningless to the person who has to act on it.
 *
 * The arithmetic is not hidden: it moves into "How this was ranked", which is
 * collapsed by default. An officer must still be able to justify the ordering
 * in a case file, so the numbers stay available - they just stop being the
 * first thing anyone has to interpret.
 */

/** One plain-language fact about the destination. */
function Fact({ label, value, detail, tone }) {
  return (
    <div className={`fact${tone ? ` fact-${tone}` : ''}`}>
      <div className="fact-label">{label}</div>
      <div className="fact-value">{value}</div>
      {detail ? <div className="fact-detail">{detail}</div> : null}
    </div>
  );
}
Fact.propTypes = {
  label: PropTypes.string.isRequired,
  value: PropTypes.node.isRequired,
  detail: PropTypes.node,
  tone: PropTypes.string,
};
Fact.defaultProps = { detail: null, tone: null };

/** The five facts an officer needs, derived from the scorer's own inputs. */
function FactGrid({ features }) {
  const pattern = activityPattern(features);
  const amount = formatINR(features.total_volume_inr);
  const lastSeen = formatDuration(features.seconds_since_last, { suffix: 'ago' });

  return (
    <div className="fact-grid">
      <Fact
        label="Amount"
        value={amount || `${Number(features.total_volume || 0).toFixed(4)} ${features.asset || ''}`}
        detail={amount ? 'Estimated from public exchange rates.' : 'No rupee rate available for this asset.'}
      />
      <Fact
        label="Number of hops"
        value={features.hop}
        detail={`The money changed wallets ${countOf(features.hop, 'time')} before arriving here.`}
      />
      <Fact
        label="Last activity"
        value={lastSeen || '—'}
        detail={features.last_seen ? <>Latest transfer <TimeAgo iso={features.last_seen} /></> : null}
      />
      <Fact
        label="Transaction frequency"
        value={countOf(features.transfer_count, 'transfer')}
        detail={`From ${countOf(features.unique_counterparties, 'sending wallet')}.`}
      />
      <Fact
        label="Activity pattern"
        value={pattern.label}
        detail={pattern.detail}
        tone={pattern.tone === 'warn' ? 'warn' : null}
      />
    </div>
  );
}
FactGrid.propTypes = { features: PropTypes.object.isRequired };

/** The route the money took, address by address.
 *
 *  The typical gap between hops is shown because it is what distinguishes
 *  money moved by a person from money moved by a script - minutes between
 *  hops is automation, days is someone deciding. It comes from the scorer's
 *  own `median_inter_hop_seconds`; when the backend does not supply it the
 *  line is omitted rather than guessed at.
 */
function Route({ path, medianHopSeconds, onSelectAddress }) {
  if (!path?.length) return null;
  const gap = formatDuration(medianHopSeconds);
  return (
    <div className="route-block">
      <h4>Shortest route to this destination</h4>
      <p className="route-lede">
        Each step is a wallet the money passed through, in order.
        {gap ? ` Typically ${gap} passed between one hop and the next.` : ''}
      </p>
      <ol className="route-list">
        {path.map((addr, i) => (
          <li className="route-item" key={`${addr}-${i}`}>
            <span className="route-step-n">
              {i === 0 ? 'Reported wallet' : i === path.length - 1 ? 'Destination' : `Hop ${i}`}
            </span>
            <AddressChip address={addr} onSelect={onSelectAddress} head={10} tail={8} />
          </li>
        ))}
      </ol>
    </div>
  );
}
Route.propTypes = {
  path: PropTypes.array,
  medianHopSeconds: PropTypes.number,
  onSelectAddress: PropTypes.func,
};
Route.defaultProps = { path: [], medianHopSeconds: null, onSelectAddress: undefined };

/**
 * The ranking arithmetic, collapsed.
 *
 * Kept because an officer has to be able to explain in a case file why one
 * destination ranked above another. Collapsed because it is not what they read
 * first.
 */
function RankingDetail({ explanation, score }) {
  if (!explanation?.length) return null;

  const NAMES = {
    volume: 'Amount received',
    hop: 'Distance from the reported wallet',
    recency: 'How recently money moved',
    frequency: 'Number of transfers',
    continuity: 'Timing consistency',
    label: 'Quality of the identification',
  };

  return (
    <details className="ranking-detail">
      <summary>How this was ranked</summary>
      <p className="ranking-lede">
        Six factors are weighed to order the destinations. The numbers below are the internal
        weighting used by the system, shown so the ranking can be explained in a case file.
      </p>
      <table className="table ranking-table">
        <thead>
          <tr><th>Factor</th><th className="num">Weighting</th></tr>
        </thead>
        <tbody>
          {explanation.map((e) => (
            <tr key={e.feature}>
              <td>{NAMES[e.feature] || e.feature}</td>
              <td className="num mono">{e.contribution.toFixed(4)}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr><td>Total ranking score</td><td className="num mono">{Number(score).toFixed(4)}</td></tr>
        </tfoot>
      </table>
    </details>
  );
}
RankingDetail.propTypes = { explanation: PropTypes.array, score: PropTypes.number };
RankingDetail.defaultProps = { explanation: [], score: 0 };

/** The wallet paid a service directly. One transaction, strongest evidence. */
function DirectExposure({ top, onSelectAddress }) {
  const ev = top.evidence || {};
  return (
    <div className="destination is-direct">
      <div className="destination-head">
        <div>
          <Badge tone="danger" uppercase>Paid directly</Badge>
          <h2 className="destination-name">{top.service}</h2>
          {top.service_type ? (
            <Badge tone={SERVICE_TONE[top.service_type] || 'neutral'}>
              {top.service_type.replace('_', ' ')}
            </Badge>
          ) : null}
        </div>
      </div>

      <div className="fact-grid">
        <Fact label="Amount" value={`${ev.amount} ${ev.asset}`} />
        <Fact label="Number of hops" value={top.hop} detail="Paid straight to this service." />
        <Fact label="When" value={<TimeAgo iso={ev.timestamp} />} />
      </div>

      <dl className="kv">
        <dt>Transaction reference</dt>
        <dd><AddressChip address={ev.txid} head={14} tail={8} /></dd>
        <dt>Paid to</dt>
        <dd><AddressChip address={ev.to_address} onSelect={onSelectAddress} /></dd>
      </dl>

      <p className="callout-inline">
        This transaction can be checked independently on any public block explorer. It is the
        line that goes in the case file.
      </p>
    </div>
  );
}
DirectExposure.propTypes = { top: PropTypes.object.isRequired, onSelectAddress: PropTypes.func };
DirectExposure.defaultProps = { onSelectAddress: undefined };

/** One ranked destination. */
function Destination({ candidate, expanded, onToggle, onSelectAddress }) {
  const f = candidate.features || {};
  const tone = SERVICE_TONE[f.service_type] || 'neutral';
  const amount = formatINR(f.total_volume_inr);

  return (
    <div className={`destination${candidate.rank === 1 ? ' is-top' : ''}`}>
      <button type="button" className="destination-head" onClick={onToggle} aria-expanded={expanded}>
        <span className="destination-rank">{candidate.rank}</span>
        <span className="grow">
          <span className="destination-title">
            <strong>{f.service}</strong>
            <Badge tone={tone}>{(f.service_type || '').replace('_', ' ')}</Badge>
          </span>
          <span className="destination-summary">
            {amount ? <strong>{amount}</strong> : `${Number(f.total_volume || 0).toFixed(4)} ${f.asset || ''}`}
            {' reached this destination after '}
            {countOf(f.hop, 'hop')}
          </span>
        </span>
        <span className="destination-caret" aria-hidden="true">{expanded ? '▾' : '▸'}</span>
      </button>

      {expanded ? (
        <div className="destination-body">
          <FactGrid features={f} />
          <Route
            path={f.shortest_path}
            medianHopSeconds={f.median_inter_hop_seconds}
            onSelectAddress={onSelectAddress}
          />
          <RankingDetail explanation={candidate.explanation} score={candidate.score} />
        </div>
      ) : null}
    </div>
  );
}
Destination.propTypes = {
  candidate: PropTypes.object.isRequired,
  expanded: PropTypes.bool,
  onToggle: PropTypes.func,
  onSelectAddress: PropTypes.func,
};
Destination.defaultProps = { expanded: false, onToggle: undefined, onSelectAddress: undefined };

export default function ExposurePanel({
  address, maxHops, onSelectAddress, tracePath, theme, selectedAddress,
}) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [openRank, setOpenRank] = useState(1);

  const load = useCallback(async () => {
    if (!address) return;
    setBusy(true);
    setError(null);
    try {
      setData(await fetchExposure(address, maxHops));
    } catch (err) {
      setError(err.message);
      setData(null);
    } finally {
      setBusy(false);
    }
  }, [address, maxHops]);

  useEffect(() => { load(); }, [load]);

  if (busy && !data) {
    return (
      <div className="panel-body stack">
        <Skeleton height="20px" width="45%" />
        <Skeleton height="90px" />
        <Skeleton height="60px" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="panel-body">
        <div className="callout callout-danger">
          <span className="glyph" aria-hidden="true">⚠</span>
          <div>{error}</div>
        </div>
      </div>
    );
  }

  if (!data) return null;

  // The money-flow diagram lives here rather than in its own tab: the graph is
  // the picture of the route the facts above describe, and separating them made
  // an investigator read the answer in one place and check it in another.
  // Drawn the way the original Money flow tab drew it: a counts toolbar with
  // pinning, then the diagram edge to edge across the panel. Investigators
  // were used to reading it at that size, and the narrower inset version made
  // long traces cramped.
  const flow = tracePath ? (
    <div className="flow-block">
      <h3>Money flow</h3>
      <div className="flow-frame">
        <div className="flow-toolbar">
          <span className="sm muted">
            {tracePath.node_count} addresses · {tracePath.link_count} transfers · depth{' '}
            {tracePath.depth}
          </span>
          <span className="grow" />
          {selectedAddress ? (
            <span className="row sm">
              <span className="muted">Pinned</span>
              <AddressChip address={selectedAddress} />
              {onSelectAddress ? (
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  onClick={() => onSelectAddress(null)}
                >
                  Clear
                </button>
              ) : null}
            </span>
          ) : (
            <span className="tiny subtle">Click a node to pin it</span>
          )}
        </div>
        <SankeyTrace
          tracePath={tracePath}
          onSelectAddress={onSelectAddress}
          selectedAddress={selectedAddress}
          theme={theme}
        />
      </div>
    </div>
  ) : null;

  if (data.kind === 'none') {
    return (
      <div className="panel-body stack" style={{ gap: 'var(--sp-5)' }}>
        <EmptyState glyph="⊘" title="The money has not reached a known service yet">
          Nothing reached an identified exchange, mixer or other service within{' '}
          {countOf(data.searched_to_hop, 'hop')}. That is a finding in itself — the funds may
          still be sitting in wallets that belong to no identified business.
        </EmptyState>
        {flow}
      </div>
    );
  }

  return (
    <div className="panel-body stack" style={{ gap: 'var(--sp-5)' }}>
      {data.kind === 'direct' ? (
        <DirectExposure top={data.top} onSelectAddress={onSelectAddress} />
      ) : (
        <>
          <p className="section-lede">
            The reported wallet did not pay a known service directly, so the money was followed
            onward. These are the destinations it reached, most significant first.
          </p>

          <div className="destinations">
            {data.candidates.map((c) => (
              <Destination
                key={`${c.features?.service}-${c.rank}`}
                candidate={c}
                expanded={openRank === c.rank}
                onToggle={() => setOpenRank(openRank === c.rank ? null : c.rank)}
                onSelectAddress={onSelectAddress}
              />
            ))}
          </div>
        </>
      )}

      {flow}

      <p className="panel-note">
        Searched up to {countOf(data.searched_to_hop, 'hop')}. Rupee amounts are estimates from
        public exchange rates, not exchange records.
      </p>
    </div>
  );
}

ExposurePanel.propTypes = {
  address: PropTypes.string,
  maxHops: PropTypes.number,
  onSelectAddress: PropTypes.func,
  tracePath: PropTypes.object,
  theme: PropTypes.string,
  selectedAddress: PropTypes.string,
};
ExposurePanel.defaultProps = {
  address: null,
  maxHops: 8,
  onSelectAddress: undefined,
  tracePath: null,
  theme: 'light',
  selectedAddress: null,
};
