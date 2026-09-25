import PropTypes from 'prop-types';
import AddressChip from './ui/AddressChip.jsx';
import { countOf } from './ui/format.js';

/**
 * How the identification was arrived at, in words rather than method names.
 *
 * Which of these applies decides how much evidentiary weight the name carries,
 * so it leads the card. A curated record and a behavioural guess look identical
 * once they are both just a company name on a screen.
 */
const METHOD = {
  tagged_db: {
    badge: 'Identified from records',
    tone: 'ok',
  },
  arkham: {
    badge: 'Identified by Arkham Intelligence',
    tone: 'ok',
  },
  classifier: {
    badge: 'Suggested from behaviour',
    tone: 'warn',
  },
  none: {
    badge: 'Not identified',
    tone: 'neutral',
  },
};

const TYPE_TONE = { mixer: 'mixer', sanctioned: 'danger' };

/** Tag sources by the name an officer would recognise and could cite. */
const SOURCE_NAMES = {
  binance_por: 'Binance Proof of Reserves',
  arkham: 'Arkham Intelligence',
  ofac_sdn: 'OFAC sanctions list',
  graphsense_ofac: 'OFAC sanctions list (GraphSense)',
  etherscan_labels: 'Etherscan labels',
  walletexplorer: 'WalletExplorer',
  graphsense_tagpacks: 'GraphSense tag packs',
  behavioural_classifier: 'Behaviour analysis',
  synthetic: 'Synthetic demo data',
};

/** Chainabuse screening of the reported wallet, as one line. */
function scamReportText(summary) {
  if (!summary || summary.status === 'not_configured') return 'Not checked (Chainabuse key not set)';
  if (summary.status === 'unavailable') return 'Not checked (Chainabuse did not respond)';
  const n = summary.report_count || 0;
  if (n === 0) return 'No reports filed';
  const cats = Object.keys(summary.categories || {}).slice(0, 3).join(', ');
  const verified = summary.verified_reports ? ` · ${summary.verified_reports} verified` : '';
  return `${countOf(n, 'report')}${verified}${cats ? ` · ${cats}` : ''}`;
}

export default function AttributionCard({ attribution, terminals, scamReports, onSelectAddress }) {
  const method = attribution?.method || 'none';
  const copy = METHOD[method] || METHOD.none;
  const clusterSize = attribution?.cluster_size;

  return (
    <div className="stack identification">
      <div className="row wrap">
        <span className={`badge badge-${copy.tone} badge-uppercase`}>{copy.badge}</span>
      </div>

      <h2 className="identification-name">
        {attribution?.entity_name
          || (method === 'classifier' ? 'Name not known' : 'Not identified')}
      </h2>


      <dl className="kv">
        {attribution?.entity_type ? (
          <>
            <dt>Type of service</dt>
            <dd>{String(attribution.entity_type).replace('_', ' ')}</dd>
          </>
        ) : null}
        {attribution?.source ? (
          <>
            <dt>Where this came from</dt>
            <dd>{SOURCE_NAMES[attribution.source] || attribution.source.replace(/_/g, ' ')}</dd>
          </>
        ) : null}
        {clusterSize ? (
          <>
            <dt>Related wallet group</dt>
            <dd>
              {countOf(clusterSize, 'wallet')}
              <div className="kv-note">
                These wallets appear to be controlled by the same person or business, because of
                the way they spend together. This is inferred from transaction patterns, not
                confirmed by any registry.
              </div>
            </dd>
          </>
        ) : null}
        {attribution?.matched_address ? (
          <>
            <dt>Matched on this wallet</dt>
            <dd><AddressChip address={attribution.matched_address} onSelect={onSelectAddress} /></dd>
          </>
        ) : null}
        {attribution?.confidence != null ? (
          <>
            <dt>Confidence</dt>
            <dd>{confidenceWord(attribution.confidence)}</dd>
          </>
        ) : null}
      </dl>

      <dl className="kv">
        <dt>Scam reports on this wallet</dt>
        <dd className={scamReports?.report_count ? 'scam-reports-hit' : undefined}>
          {scamReportText(scamReports)}
          {scamReports?.status === 'checked' && scamReports.url ? (
            <>
              {' '}
              <a href={scamReports.url} target="_blank" rel="noreferrer noopener">View on Chainabuse</a>
            </>
          ) : null}
        </dd>
      </dl>

      {terminals && terminals.length > 0 ? (
        <div className="reached-block">
          <h4>Services this money reached</h4>
          <div className="stack" style={{ gap: 'var(--sp-2)' }}>
            {terminals.map((t) => (
              <div className="reached-row" key={`${t.address}-${t.hop}`}>
                <span className={`dot dot-${TYPE_TONE[t.entity_type] || 'ok'}`} />
                <strong>{t.entity_name}</strong>
                <span className="reached-meta">
                  {String(t.entity_type).replace('_', ' ')} · after {countOf(t.hop, 'hop')}
                </span>
              </div>
            ))}
          </div>
        </div>
      ) : null}

      <div className="what-this-means">
        <h4>What this means</h4>
        <p>
          This section shows what the reported wallet appears to be connected to, based on its
          transaction activity and on published records of known services.
        </p>
        <p>
          These are <strong>indicators, not proof of ownership</strong>. A wallet identified as
          belonging to an exchange means the money reached that exchange — it does not mean the
          exchange was involved in the fraud. Confirming who actually controls a wallet requires
          a legal request to the service that holds the customer records.
        </p>
      </div>
    </div>
  );
}

/** A confidence figure as a word. "0.62" tells an officer nothing on its own. */
function confidenceWord(value) {
  const n = Number(value);
  if (Number.isNaN(n)) return 'Unknown';
  if (n >= 0.9) return 'Very strong';
  if (n >= 0.7) return 'Strong';
  if (n >= 0.5) return 'Moderate';
  if (n > 0) return 'Weak';
  return 'None';
}

AttributionCard.propTypes = {
  attribution: PropTypes.shape({
    method: PropTypes.string,
    entity_name: PropTypes.string,
    entity_type: PropTypes.string,
    confidence: PropTypes.number,
    source: PropTypes.string,
    cluster_size: PropTypes.number,
    matched_address: PropTypes.string,
    note: PropTypes.string,
  }),
  terminals: PropTypes.arrayOf(PropTypes.object),
  scamReports: PropTypes.shape({
    status: PropTypes.string,
    report_count: PropTypes.number,
    verified_reports: PropTypes.number,
    categories: PropTypes.object,
    url: PropTypes.string,
  }),
  onSelectAddress: PropTypes.func,
};
AttributionCard.defaultProps = {
  attribution: null, terminals: [], scamReports: null, onSelectAddress: undefined,
};
