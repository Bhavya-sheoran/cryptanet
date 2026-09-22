import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../api/client.js', () => ({ fetchExposure: vi.fn() }));

// The sankey pulls d3 into jsdom, which has no layout. The flow diagram is
// covered by its own component; here we only care that the panel renders it
// in place rather than sending the officer to a separate tab.
vi.mock('./SankeyTrace.jsx', () => ({
  default: () => <svg data-testid="sankey" />,
}));

const { fetchExposure } = await import('../api/client.js');
const ExposurePanel = (await import('./ExposurePanel.jsx')).default;

/**
 * The exposure answer as an officer with no blockchain background reads it.
 *
 * EXPOSURE is the verbatim body of GET /api/v1/exposure for the TRON demo
 * wallet, captured from the running stack. Using the real payload is the
 * point: the redesign replaced model-facing labels with plain language, and
 * a fixture invented to match the new labels would pass while the real field
 * names moved underneath it.
 */
const EXPOSURE = {
  kind: 'indirect',
  chain: 'TRON',
  address: 'TFxXYrP93fNJjoFKa2EaVU8UM3FqabDJ8a',
  searched_to_hop: 8,
  scoring_version: 'exposure-v1',
  exposure_id: 'f45c9b99-3c96-4087-9d02-577aa6c78ed0',
  data_provenance: 'synthetic',
  explanation: 'Ranked 1 candidate service by an explainable score.',
  notice:
    'Recommendation only. Service attribution rests on public labels and an '
    + 'explainable score, not on exchange KYC. Any freeze or disclosure request '
    + 'requires explicit approval by an authorised officer.',
  top: null,
  candidates: [
    {
      rank: 1,
      score: 0.5638,
      scoring_version: 'exposure-v1',
      features: {
        service: 'Meridian Exchange',
        service_type: 'exchange',
        hop: 7,
        path_count: 1,
        shortest_path: [
          'TFxXYrP93fNJjoFKa2EaVU8UM3FqabDJ8a',
          'TUQmoG7ktMRqMBvMToLokkQuNRG2nar427',
          'TSfq4C7hwiMHBYBgMB9kdxm5xNjpNjdTJG',
          'TKN8ZhMgqwwezEwDicm6jxzXDRofLy24X8',
          'TKpohYt5MSumqaEGHoWexJbg9N9gARYhd5',
          'TWQJMJ96kDXSfUXVBrZJqW9deQY8ZZ91uW',
          'TFky7HReszf9cpZGCscvep3gVv9aZMr7J3',
          'TRyRpB9pg4aegknQoXD4HpBJZN9xY4zqub',
        ],
        total_volume: 22770.604569,
        max_transfer: 22770.604569,
        transfer_count: 1,
        unique_counterparties: 1,
        first_seen: '2026-09-02T18:24:00.719582+00:00',
        last_seen: '2026-09-02T18:24:00.719582+00:00',
        seconds_since_last: 769935.184504,
        median_inter_hop_seconds: 7800.0,
        continuity_ok: true,
        label_confidence: 1.0,
        label_sources: ['synthetic'],
        asset: 'USDT',
        mixed_assets: false,
        total_volume_inr: 2003813.202072,
        max_transfer_inr: 2003813.202072,
        price_sources: ['synthetic'],
        priced: true,
        volume_basis: 'INR',
      },
      explanation: [
        { feature: 'volume', raw: 2003813.202072, normalised: 0.8255, weight: 0.24, contribution: 0.1981 },
        { feature: 'label', raw: 1.0, normalised: 1.0, weight: 0.16, contribution: 0.16 },
        { feature: 'continuity', raw: true, normalised: 1.0, weight: 0.1, contribution: 0.1 },
        { feature: 'recency', raw: 769935.184504, normalised: 0.4138, weight: 0.16, contribution: 0.0662 },
        { feature: 'hop', raw: 7, normalised: 0.125, weight: 0.22, contribution: 0.0275 },
        { feature: 'frequency', raw: 1, normalised: 0.1, weight: 0.12, contribution: 0.012 },
      ],
    },
  ],
};

function renderPanel(payload = EXPOSURE) {
  fetchExposure.mockResolvedValue(payload);
  return render(<ExposurePanel address={EXPOSURE.address} maxHops={8} />);
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('ExposurePanel', () => {
  it('answers in rupees and hops, not in model features', async () => {
    renderPanel();

    // The five facts that replaced the scorer's feature names.
    expect(await screen.findByText('Amount')).toBeInTheDocument();
    expect(screen.getByText('Number of hops')).toBeInTheDocument();
    expect(screen.getByText('Last activity')).toBeInTheDocument();
    expect(screen.getByText('Transaction frequency')).toBeInTheDocument();
    expect(screen.getByText('Activity pattern')).toBeInTheDocument();

    // The values, derived from the payload rather than hardcoded.
    expect(screen.getAllByText('₹20.04 lakh').length).toBeGreaterThan(0);
    expect(screen.getByText('8 days 21 hours ago')).toBeInTheDocument();
    expect(screen.getByText('1 transfer')).toBeInTheDocument();
    expect(screen.getByText('Single transfer')).toBeInTheDocument();
  });

  it('does not put a raw model score in front of the officer', async () => {
    const { container } = renderPanel();
    await screen.findByText('Amount');

    // "0.1981" is meaningless to the reader. It is allowed inside the
    // collapsed ranking detail, but must not be sitting in the open page.
    const openText = Array.from(container.querySelectorAll('*'))
      .filter((el) => !el.closest('details') || el.closest('details')?.open)
      .map((el) => el.textContent)
      .join(' ');
    expect(openText).not.toMatch(/\b0\.\d{4}\b/);

    // And the model-facing vocabulary is gone from the visible copy.
    expect(screen.queryByText(/^Continuity$/i)).toBeNull();
    expect(screen.queryByText(/^Recency$/i)).toBeNull();
    expect(screen.queryByText(/^Label confidence$/i)).toBeNull();
  });

  it('keeps the ranking arithmetic available but collapsed, for the case file', async () => {
    const { container } = renderPanel();
    await screen.findByText('Amount');

    const details = container.querySelector('details.ranking-detail')
      ?? container.querySelector('details');
    expect(details).toBeTruthy();
    // Collapsed by default: defensible, not the first thing read.
    expect(details.open).toBe(false);

    await userEvent.click(within(details).getByText(/how this was ranked/i));
    await waitFor(() => expect(details.open).toBe(true));

    // The decimals an officer would need to justify the ordering are intact.
    expect(within(details).getByText(/0\.1981/)).toBeInTheDocument();
  });

  it('shows the route with the typical gap between hops', async () => {
    renderPanel();
    await screen.findByText('Amount');

    expect(screen.getByText(/shortest route to this destination/i)).toBeInTheDocument();
    // median_inter_hop_seconds = 7800, rendered in plain English.
    expect(screen.getByText(/2 hours 10 minutes passed between one hop and the next/i))
      .toBeInTheDocument();

    // First and last steps are named, not numbered from zero.
    expect(screen.getByText('Reported wallet')).toBeInTheDocument();
    expect(screen.getByText('Destination')).toBeInTheDocument();
  });

  it('renders the money flow beside the answer rather than in a separate tab', async () => {
    // The diagram is the picture of the route the facts describe, so it
    // renders inside the same answer - but only when there is a trace to
    // draw. No trace, no diagram; it is never a decorative empty box.
    fetchExposure.mockResolvedValue(EXPOSURE);
    const { rerender } = render(<ExposurePanel address={EXPOSURE.address} maxHops={8} />);
    await screen.findByText('Amount');
    expect(screen.queryByTestId('sankey')).toBeNull();

    rerender(
      <ExposurePanel
        address={EXPOSURE.address}
        maxHops={8}
        tracePath={{ nodes: [], links: [], node_count: 12, link_count: 11, depth: 8 }}
      />,
    );
    expect(await screen.findByTestId('sankey')).toBeInTheDocument();
    expect(screen.getByText('Money flow')).toBeInTheDocument();
    // The original Money flow toolbar, restored: counts plus the pin hint.
    expect(screen.getByText(/12 addresses · 11 transfers · depth\s+8/)).toBeInTheDocument();
    expect(screen.getByText('Click a node to pin it')).toBeInTheDocument();
  });

  it('does not claim the rupee figures came from exchange records', async () => {
    renderPanel();
    await screen.findByText('Amount');

    // The standing constraint: never imply access to exchange KYC. The
    // amounts are priced from public rates and must say so.
    expect(screen.getByText(/estimates from\s+public exchange rates, not exchange records/i))
      .toBeInTheDocument();
  });

  it('says plainly when nothing was found, instead of showing an empty ranking', async () => {
    renderPanel({
      ...EXPOSURE,
      kind: 'none',
      candidates: [],
      top: null,
      explanation: 'No service exposure found within the searched depth.',
    });

    // The API's own wording is jargon; the panel states the finding in the
    // terms an officer needs - including that "nothing found" is itself a
    // result rather than a failure.
    expect(await screen.findByText(/has not reached a known service yet/i)).toBeInTheDocument();
    expect(screen.getByText(/a finding in itself/i)).toBeInTheDocument();
    expect(screen.queryByText('Number of hops')).toBeNull();
  });

  it('surfaces a failed lookup rather than rendering a blank panel', async () => {
    fetchExposure.mockRejectedValue(new Error('Service exposure lookup failed (503).'));
    render(<ExposurePanel address={EXPOSURE.address} maxHops={8} />);

    expect(await screen.findByText(/503/)).toBeInTheDocument();
  });
});
