import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import AttributionCard from './AttributionCard.jsx';

/**
 * The identification card names where an identification came from in words an
 * officer can cite, and never shows "no scam reports" for a check that did
 * not run.
 */
describe('AttributionCard', () => {
  it('names the source of an identification', () => {
    render(
      <AttributionCard
        attribution={{ method: 'tagged_db', entity_name: 'Binance', entity_type: 'exchange',
          source: 'binance_por', confidence: 1 }}
      />,
    );
    expect(screen.getByText('Binance Proof of Reserves')).toBeInTheDocument();
  });

  it('marks an Arkham identification as such', () => {
    render(
      <AttributionCard
        attribution={{ method: 'arkham', entity_name: 'Kraken', entity_type: 'exchange',
          source: 'arkham', confidence: 0.85 }}
      />,
    );
    expect(screen.getByText('Identified by Arkham Intelligence')).toBeInTheDocument();
    expect(screen.getByText('Arkham Intelligence')).toBeInTheDocument();
  });

  it('says "not checked" rather than "no reports" without a Chainabuse key', () => {
    render(<AttributionCard attribution={{ method: 'none' }} scamReports={{ status: 'not_configured' }} />);
    expect(screen.getByText(/not checked/i)).toBeInTheDocument();
    expect(screen.queryByText(/no reports filed/i)).toBeNull();
  });

  it('shows how many scam reports were filed, with a link to them', () => {
    render(
      <AttributionCard
        attribution={{ method: 'none' }}
        scamReports={{ status: 'checked', report_count: 4, verified_reports: 2,
          categories: { PIG_BUTCHERING: 3, PHISHING: 1 }, url: 'https://www.chainabuse.com/address/x' }}
      />,
    );
    expect(screen.getByText(/4 reports · 2 verified · PIG_BUTCHERING, PHISHING/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'View on Chainabuse' }))
      .toHaveAttribute('href', 'https://www.chainabuse.com/address/x');
  });
});
