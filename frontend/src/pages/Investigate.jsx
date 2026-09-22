import { useCallback, useEffect, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import WalletInputForm from '../components/WalletInputForm.jsx';
import ExposurePanel from '../components/ExposurePanel.jsx';
import RiskPanel from '../components/RiskPanel.jsx';
import ContributingCases from '../components/ContributingCases.jsx';
import AttributionCard from '../components/AttributionCard.jsx';
import CaseWorkspace from '../components/CaseWorkspace.jsx';
import Tabs from '../components/ui/Tabs.jsx';
import AddressChip from '../components/ui/AddressChip.jsx';
import { Callout, EmptyState, RiskBadge, Skeleton, Stat } from '../components/ui/Bits.jsx';
import { countOf } from '../components/ui/format.js';
import useTheme from '../components/ui/useTheme.js';
import { useToast } from '../components/ui/toast-context.js';
import { analyseWallet, fetchRankedExchanges, submitWallet } from '../api/client.js';

/**
 * The investigator's workspace: report a wallet, follow the money, see who the
 * funds reached and how confident that attribution is, and read the cases
 * behind the risk score.
 *
 * The detail is tabbed rather than stacked. A trace produces four different
 * kinds of evidence - the flow, the attribution, the score, the case file - and
 * stacking them means scrolling past three to reach the fourth.
 */
export default function Investigate({ currentUser, submitted, onOpenCase }) {
  const { theme } = useTheme();
  const toast = useToast();
  const [analysis, setAnalysis] = useState(null);
  const [intake, setIntake] = useState(null);
  const [scoring, setScoring] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(null);
  const [tab, setTab] = useState('exposure');
  const runRef = useRef(null);

  useEffect(() => {
    if (!currentUser) return undefined;
    let cancelled = false;
    fetchRankedExchanges(1)
      .then((d) => !cancelled && setScoring(d.scoring || null))
      .catch(() => {});
    return () => { cancelled = true; };
  }, [currentUser]);

  const runAnalysis = useCallback(async (address) => {
    setBusy(true);
    setError(null);
    setSelected(null);
    try {
      setAnalysis(await analyseWallet(address));
      setTab('exposure');
    } catch (err) {
      setAnalysis(null);
      setError(
        err.status === 401
          ? 'Sign in to analyse a wallet. The result names contributing case numbers, so it is not available anonymously.'
          : err.status === 404
            ? 'No graph data for this address yet. File the complaint first so the flow can be traced.'
            : err.message,
      );
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => { runRef.current = runAnalysis; }, [runAnalysis]);

  // A search from the top bar drives the analysis.
  useEffect(() => {
    if (submitted?.address) runRef.current?.(submitted.address);
  }, [submitted]);

  const handleSubmit = useCallback(
    async (payload) => {
      setBusy(true);
      setError(null);
      try {
        const result = await submitWallet(payload);
        setIntake(result);
        toast(`Case ${result.case_number} opened · ${result.trace.hops_discovered} hops traced`);
        setAnalysis(await analyseWallet(payload.address));
        setTab('exposure');
        return result;
      } catch (err) {
        setError(err.message);
        toast(err.message, 'error');
        return null;
      } finally {
        setBusy(false);
      }
    },
    [toast],
  );

  const caseId = intake?.case_id || analysis?.reported_in_cases?.[0]?.case_id;

  return (
    <>
      <div className="page-head">
        <div>
          <div className="crumb">Workspace</div>
          <h1>Investigate a suspect wallet</h1>
        </div>
      </div>

      <section className="panel">
        <div className="panel-head"><h2>Report a suspect wallet</h2></div>
        <div className="panel-body">
          <WalletInputForm onAnalyse={runAnalysis} onSubmitted={handleSubmit} busy={busy} />
        </div>
      </section>

      {error ? <Callout tone="danger" glyph="⚠">{error}</Callout> : null}

      {intake?.duplicate?.is_duplicate ? (
        <Callout tone="warn" glyph="⚑">
          <strong>Repeat report — possible fraud ring.</strong> {intake.duplicate.note}
          <div className="tiny mono" style={{ marginTop: 4 }}>
            Earlier cases: {intake.duplicate.prior_case_numbers.join(', ')}
          </div>
        </Callout>
      ) : null}

      {busy && !analysis ? (
        <section className="panel">
          <div className="panel-body stack">
            <Skeleton height="18px" width="40%" />
            <Skeleton height="140px" />
            <Skeleton height="18px" />
          </div>
        </section>
      ) : null}

      {analysis ? (
        <>
          <div className="stat-row">
            <Stat
              label="Risk level"
              value={<RiskBadge label={analysis.risk_label} />}
              sub={`Based on ${countOf(analysis.contributing_case_count, 'reported case')}`}
            />
            <Stat
              label="Money reached"
              value={analysis.attribution?.entity_name || 'Not yet identified'}
              sub={
                analysis.attribution?.method === 'tagged_db'
                  ? 'Identified from records'
                  : analysis.attribution?.method === 'classifier'
                    ? 'Suggested from behaviour'
                    : 'No identification'
              }
            />
            <Stat
              label="Wallets involved"
              value={analysis.trace_path.node_count}
              sub={`${analysis.trace_path.link_count} transfers followed`}
            />
            <Stat label="Network" value={analysis.chain} sub="Blockchain the funds moved on" />
          </div>

          <div className="subject-line">
            <span className="subject-label">Reported wallet</span>
            <AddressChip
              address={analysis.address}
              full
              entityName={analysis.attribution?.entity_name}
              entityType={analysis.attribution?.entity_type}
            />
          </div>

          {analysis.mixer_interaction ? (
            <Callout tone="mixer" glyph="⚠">
              <strong>The money passed through a mixing service.</strong> A mixer is used to break
              the trail between sender and receiver. The funds cannot be followed past that point,
              and this itself is a strong indicator of deliberate laundering.
            </Callout>
          ) : null}

          <section className="panel">
            <Tabs
              active={tab}
              onChange={setTab}
              tabs={[
                { id: 'exposure', label: 'Where the money went' },
                { id: 'attribution', label: 'Wallet identification' },
                { id: 'risk', label: 'Risk', count: analysis.contributing_case_count },
                { id: 'case', label: 'Case file' },
              ]}
            />

            {tab === 'exposure' ? (
              <ExposurePanel
                address={analysis.address}
                maxHops={analysis.trace_path.depth}
                onSelectAddress={setSelected}
                tracePath={analysis.trace_path}
                theme={theme}
                selectedAddress={selected}
              />
            ) : null}

            {tab === 'attribution' ? (
              <div className="panel-body">
                <AttributionCard
                  attribution={analysis.attribution}
                  terminals={analysis.terminal_attributions}
                  onSelectAddress={setSelected}
                />
              </div>
            ) : null}

            {tab === 'risk' ? (
              <div className="panel-body stack" style={{ gap: 'var(--sp-6)' }}>
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
              </div>
            ) : null}

            {tab === 'case' ? (
              currentUser ? (
                <CaseWorkspace
                  caseId={caseId}
                  currentUser={currentUser}
                  targetAddress={analysis.terminal_attributions?.[0]?.address}
                  entityName={analysis.attribution?.entity_name}
                  embedded
                />
              ) : (
                <div className="panel-body">
                  <EmptyState glyph="🔒" title="Sign in to open the case file">
                    Notes, evidence, the hashed forensic report, STR drafts and freeze requests are
                    available to a signed-in officer. Approving a freeze additionally requires a
                    supervisor.
                  </EmptyState>
                </div>
              )
            ) : null}

          </section>
        </>
      ) : !busy && !error ? (
        <section className="panel">
          <div className="panel-body">
            <EmptyState glyph="⌕" title="No wallet analysed yet">
              Paste a suspect address above, or search from the bar at the top. Press <kbd>/</kbd>{' '}
              anywhere to jump to search.
            </EmptyState>
          </div>
        </section>
      ) : null}
    </>
  );
}

Investigate.propTypes = {
  currentUser: PropTypes.shape({ role: PropTypes.string, can_approve: PropTypes.bool }),
  submitted: PropTypes.shape({ address: PropTypes.string, at: PropTypes.number }),
  onOpenCase: PropTypes.func,
};
Investigate.defaultProps = { currentUser: null, submitted: null, onOpenCase: undefined };
