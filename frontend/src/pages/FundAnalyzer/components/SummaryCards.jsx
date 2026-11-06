import { CircleDot, Shield, Star, TrendingUp } from 'lucide-react';
import { signedPct } from '../fundAnalysis.js';

function Card({ icon: Icon, label, value, caption, dark, overlapCard }) {
  return <div className={`fa-stat${dark ? ' fa-stat--dark' : ''}${overlapCard ? ' fa-stat--overlap' : ''}`}><span className="fa-stat__label"><Icon size={13} /> {label}</span><strong className="fa-stat__value">{value}</strong><span className="fa-stat__caption">{caption}</span></div>;
}

export default function SummaryCards({ funds, overlap, overlapLoading, overlapStage }) {
  const valid = funds.filter((fund) => fund.metrics);
  const top = (key, direction) => valid.reduce((best, fund) => {
    if (!best) return fund;
    const a = fund.metrics?.[key]; const b = best.metrics?.[key];
    if (a == null) return best;
    if (b == null) return fund;
    return direction === 'low' ? (a < b ? fund : best) : (a > b ? fund : best);
  }, null);
  const topCagr = top('r3y', 'high'); const bestSharpe = top('sharpe', 'high'); const lowestVol = top('volatility', 'low');
  const highest = overlap?.highest_overlap;
  const leftFund = funds.find((fund) => fund.scheme_code === highest?.fund_a_code);
  const rightFund = funds.find((fund) => fund.scheme_code === highest?.fund_b_code);
  const portfolioMonth = highest?.portfolio_month || highest?.portfolio_date?.slice(0, 7);
  const portfolioLabel = portfolioMonth
    ? new Date(`${portfolioMonth}-01T00:00:00Z`).toLocaleString('en-US', { month: 'long', year: 'numeric', timeZone: 'UTC' })
    : null;
  const overlapCaption = overlapLoading
    ? (overlapStage || 'Fetching portfolio holdings...')
    : highest?.available
      ? <><i style={{ background: leftFund?.color }} /><i style={{ background: rightFund?.color }} /><span className="fa-stat__overlap-detail"><span>{highest.fund_a_name} ↔ {highest.fund_b_name}</span>{highest.overlap_type === 'DIRECT_FOF' && <small>{highest.overlap_label} (common underlying fund units)</small>}{portfolioLabel && <small>Portfolio as of {portfolioLabel}</small>}{highest.holdings_source && <small>Holdings source: {highest.holdings_source === 'official_amc' ? 'Official AMC' : highest.holdings_source === 'charlie_alpha' ? 'Third-party API' : 'Official AMC and Third-party API'}</small>}{highest.partial_coverage && <small>Partial coverage: {highest.compared_pairs}/{highest.total_pairs} pairs</small>}{highest.pairs_different_months && <small>Pairs use different reporting months</small>}{highest.message && <small>{highest.message}</small>}</span></>
      : highest?.message || 'Portfolio holdings unavailable';
  return <section className="fa-stats">
    <Card dark icon={TrendingUp} label="TOP 3Y CAGR" value={signedPct(topCagr?.metrics?.r3y)} caption={topCagr?.scheme_name || 'Not enough history'} />
    <Card icon={Star} label="BEST RISK-ADJUSTED" value={bestSharpe?.metrics?.sharpe == null ? '—' : <>{bestSharpe.metrics.sharpe.toFixed(2)} <small>Sharpe</small></>} caption={bestSharpe?.scheme_name || 'Not enough history'} />
    <Card icon={Shield} label="LOWEST VOLATILITY" value={lowestVol?.metrics?.volatility == null ? '—' : `${(lowestVol.metrics.volatility * 100).toFixed(2)}%`} caption={lowestVol?.scheme_name || 'Not enough history'} />
    <Card overlapCard icon={CircleDot} label="HIGHEST OVERLAP" value={overlapLoading ? '...' : highest?.available ? `${highest.percentage.toFixed(2)}%` : '—'} caption={overlapCaption} />
  </section>;
}
