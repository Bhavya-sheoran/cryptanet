import { useCallback, useEffect, useState } from 'react';
import PropTypes from 'prop-types';
import ExposurePanel from '../components/ExposurePanel.jsx';
import AttributionCard from '../components/AttributionCard.jsx';
import RiskPanel from '../components/RiskPanel.jsx';
import ContributingCases from '../components/ContributingCases.jsx';
import CaseWorkspace from '../components/CaseWorkspace.jsx';
import Tabs from '../components/ui/Tabs.jsx';
import AddressChip from '../components/ui/AddressChip.jsx';
import { Badge, Callout, EmptyState, RiskBadge, Skeleton, Stat } from '../components/ui/Bits.jsx';
import { countOf, formatINR } from '../components/ui/format.js';
import useTheme from '../components/ui/useTheme.js';
import { analyseWallet, fetchCase, fetchRankedExchanges } from '../api/client.js';

const STATUS_WORD = {
  open: 'Open',
  tracing: 'Tracing',
  analysed: 'Analysed',
  escalated: 'Escalated',
  closed: 'Closed',
};

/**
 * One case, investigated in full, on its own page.
 *
 * This replaces the half-width panel the case list used to open beside itself.
 * Investigating a case is the main task, not a preview of one - an officer
 * reading a money flow, a route and an evidence list needs the whole screen,
 * and a narrow column forced every table into a horizontal scroll.
 *
 * Nothing here is new analysis. It assembles the components the Investigate
 * page already uses, around the case record they belong to.
 */
export default function CaseDashboard({ caseId, currentUser, onBack, onOpenCase }) {
  const { theme } = useTheme();
  const [detail, setDetail] = useState(null);
  const [analysis, setAnalysis] = useState(null);
  const [scoring, setScoring] = useState(null);
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(null);
  const [tab, setTab] = useState('exposure');

  const load = useCallback(async () => {
    if (!caseId) return;
    setError(null);
    try {
      const c = await fetchCase(caseId);
      setDetail(c);

      // The wallet the complaint named. Its analysis is what turns a case
      // record into an investigation.
      const address = c.wallets?.[0]?.address;
      if (address) {
        try {
          setAnalysis(await analyseWallet(address));
        } catch {
          // A case can exist before its wallet has any traced flow. The case
          // record is still worth showing, so this is not fatal.
          setAnalysis(null);
        }
      }
    } catch (err) {
      setError(err.message);
    }
  }, [caseId]);

  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    let cancelled = false;
    fetchRankedExchanges(1)
      .then((d) => !cancelled && setScoring(d.scoring || null))
      .catch(() => {});
    return () => { cancelled = true; };
  }, []);

  if (error) {
    return (
      <>
        <BackLink onBack={onBack} />
        <Callout tone="danger" glyph="⚠">{error}</Callout>
      </>
    );
  }

  if (!detail) {
    return (
      <>
        <BackLink onBack={onBack} />
        <section className="panel">
          <div className="panel-body stack">
            <Skeleton height="20px" width="40%" />
            <Skeleton height="120px" />
          </div>
        </section>
      </>
    );
  }

  const wallet = detail.wallets?.[0];
  const trace = detail.traces?.[0];

  return (
    <>
      <BackLink onBack={onBack} />

      <div className="page-head">
        <div>
          <div className="crumb">Case</div>
          <h1>{detail.case_number}</h1>
          <p className="lede">
            Complaint received {(detail.reported_at || '').slice(0, 10)}
            {detail.amount_inr != null ? ` · ${formatINR(detail.amount_inr)} reported lost` : ''}
          </p>
        </div>
        <Badge tone={detail.status === 'escalated' ? 'danger' : 'info'}>
          {STATUS_WORD[detail.status] || detail.status}
        </Badge>
      </div>

      <div className="stat-row">
        <Stat
          label="Risk level"
          value={analysis ? <RiskBadge label={analysis.risk_label} /> : '—'}
          sub={
            analysis
              ? `Based on ${countOf(analysis.contributing_case_count, 'reported case')}`
              : 'Not yet analysed'
          }
        />
        <Stat
          label="Amount reported lost"
          value={formatINR(detail.amount_inr) || '—'}
          sub="As stated in the complaint"
        />
        <Stat
          label="Money reached"
          value={analysis?.attribution?.entity_name || 'Not yet identified'}
          sub={
            analysis?.attribution?.method === 'tagged_db'
              ? 'Identified from records'
              : analysis?.attribution?.method === 'classifier'
                ? 'Suggested from behaviour'
                : 'No identification'
          }
        />
        <Stat
          label="Wallets involved"
          value={analysis?.trace_path?.node_count ?? trace?.addresses_touched ?? '—'}
          sub={trace ? `${countOf(trace.hops_discovered, 'hop')} followed` : 'No trace yet'}
        />
      </div>

      {wallet ? (
        <div className="subject-line">
          <span className="subject-label">Reported wallet</span>
          <AddressChip
            address={wallet.address}
            full
            entityName={analysis?.attribution?.entity_name}
            entityType={analysis?.attribution?.entity_type}
          />
          <Badge tone="neutral">{wallet.chain}</Badge>
        </div>
      ) : null}

      {detail.narrative ? (
        <section className="panel">
          <div className="panel-head"><h2>What the victim reported</h2></div>
          <div className="panel-body">
            <p className="narrative-text">{detail.narrative}</p>
          </div>
        </section>
      ) : null}

      {analysis?.mixer_interaction ? (
        <Callout tone="mixer" glyph="⚠">
          <strong>The money passed through a mixing service.</strong> A mixer is used to break the
          trail between sender and receiver. The funds cannot be followed past that point, and this
          itself is a strong indicator of deliberate laundering.
        </Callout>
      ) : null}

      <section className="panel">
        <Tabs
          active={tab}
          onChange={setTab}
          tabs={[
            { id: 'exposure', label: 'Where the money went' },
            { id: 'attribution', label: 'Wallet identification' },
            { id: 'risk', label: 'Risk', count: analysis?.contributing_case_count },
            { id: 'case', label: 'Case file' },
          ]}
        />

        {tab === 'exposure' ? (
          analysis ? (
            <ExposurePanel
              address={analysis.address}
              maxHops={analysis.trace_path.depth}
              onSelectAddress={setSelected}
              tracePath={analysis.trace_path}
              theme={theme}
              selectedAddress={selected}
            />
          ) : (
            <div className="panel-body">
              <EmptyState glyph="⊘" title="This wallet has not been traced yet">
                No money flow has been recorded for the wallet on this case.
              </EmptyState>
            </div>
          )
        ) : null}

        {tab === 'attribution' ? (
          <div className="panel-body">
            {analysis ? (
              <AttributionCard
                attribution={analysis.attribution}
                terminals={analysis.terminal_attributions}
                onSelectAddress={setSelected}
              />
            ) : (
              <EmptyState glyph="⊘" title="Nothing to identify yet">
                The wallet on this case has not been traced.
              </EmptyState>
            )}
          </div>
        ) : null}

        {tab === 'risk' ? (
          <div className="panel-body stack" style={{ gap: 'var(--sp-6)' }}>
            {analysis ? (
              <>
                <RiskPanel
                  label={analysis.risk_label}
                  score={analysis.risk_score}
                  factors={analysis.risk_factors}
                  halfLifeDays={scoring?.half_life_days}
                />
                <div>
                  <h4>Complaints behind this rating</h4>
                  <ContributingCases
                    contributions={analysis.contributions}
                    totalScore={analysis.risk_score}
                    total={analysis.contributing_case_count}
                    onOpenCase={onOpenCase}
                  />
                </div>
              </>
            ) : (
              <EmptyState glyph="⊘" title="No risk rating yet">
                A rating is produced once the wallet on this case has been traced.
              </EmptyState>
            )}
          </div>
        ) : null}

        {tab === 'case' ? (
          <CaseWorkspace
            caseId={caseId}
            currentUser={currentUser}
            targetAddress={analysis?.terminal_attributions?.[0]?.address || wallet?.address}
            entityName={analysis?.attribution?.entity_name}
            embedded
          />
        ) : null}
      </section>
    </>
  );
}

function BackLink({ onBack }) {
  if (!onBack) return null;
  return (
    <button type="button" className="btn btn-ghost btn-sm back-link" onClick={onBack}>
      ← Back
    </button>
  );
}
BackLink.propTypes = { onBack: PropTypes.func };
BackLink.defaultProps = { onBack: undefined };

CaseDashboard.propTypes = {
  caseId: PropTypes.string.isRequired,
  currentUser: PropTypes.shape({ role: PropTypes.string, can_approve: PropTypes.bool }),
  onBack: PropTypes.func,
  onOpenCase: PropTypes.func,
};
CaseDashboard.defaultProps = { currentUser: null, onBack: undefined, onOpenCase: undefined };
