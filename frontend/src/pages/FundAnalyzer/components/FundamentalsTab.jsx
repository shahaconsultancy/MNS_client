import { useState } from 'react';
import { ArrowRight, ChevronLeft, Info, BarChart3, ListOrdered, PieChart } from 'lucide-react';
import { crore, nav, signedPct } from '../fundAnalysis.js';

const CARD_HOLDINGS = 5;
const DETAIL_HOLDINGS = 10;

const shortName = (name) => (name || '').split(' - ')[0];
const longDate = (value) => (value
  ? new Date(`${value}T00:00:00Z`).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' })
  : null);
const isPending = (fund, loading) => loading && fund.holdings === undefined;
const expense = (fund, pending) => (fund.ter == null ? (pending ? '…' : '—') : `${fund.ter.toFixed(2)}%`);
const aum = (fund, pending) => (fund.aum == null && pending ? '…' : crore(fund.aum));
const upDown = (value) => (value == null ? '' : value >= 0 ? 'fa-up' : 'fa-down');
const sourceDates = (fund) => [
  fund.holdings_as_of && `Portfolio ${longDate(fund.holdings_as_of)}${fund.holdings_source ? ` (Holdings source: ${fund.holdings_source === 'charlie_alpha' ? 'Third-party API' : 'Official AMC'})` : ''}`,
  fund.ter_as_of && `Expense ${longDate(fund.ter_as_of)}`,
  fund.aum_as_of && `AUM quarter to ${longDate(fund.aum_as_of)}`,
].filter(Boolean).join(' · ');

const debtDetails = (holding) => [holding.instrument_type, holding.sector, holding.issuer, holding.rating, holding.excluded_from_overlap && 'Not in overlap'].filter(Boolean).join(' · ');

function Holdings({ holdings, pending, limit, showDetails }) {
  if (pending) return <p className="fa-fund__empty">Loading holdings…</p>;
  if (!holdings?.length) return <p className="fa-fund__empty">Holdings are not available for this fund.</p>;
  const largest = holdings[0].weight || 1;
  return <ol className={`fa-fund__holdings${limit == null ? ' fa-fund__holdings--all' : ''}`}>
    {(limit == null ? holdings : holdings.slice(0, limit)).map((holding, index) => <li key={holding.isin || holding.name} className="fa-fund__holding">
      <span className="fa-fund__rank">{String(index + 1).padStart(2, '0')}</span>
      <div>
        <p><span title={[holding.isin, debtDetails(holding)].filter(Boolean).join(' · ')}>{holding.name}</span><strong>{holding.weight.toFixed(2)}%</strong></p>
        {showDetails && debtDetails(holding) && <small className="fa-fund__holding-meta">{debtDetails(holding)}</small>}
        <i><b style={{ width: `${(holding.weight / largest) * 100}%` }} /></i>
      </div>
    </li>)}
  </ol>;
}

function RiskBadge({ fund, pending }) {
  const risk = fund.riskometer;
  if (!risk) return <span className="fa-fund__risk fa-fund__risk--none">{pending ? 'Loading risk…' : 'Risk Not Available'}</span>;
  return <span className={`fa-fund__risk fa-fund__risk--${risk.level.toLowerCase().replaceAll(' ', '-')}`} title={`SEBI Riskometer · ${risk.source} · as of ${longDate(risk.as_of)}`}>{risk.level}</span>;
}

const topShare = (holdings, count) => holdings.slice(0, count).reduce((sum, holding) => sum + holding.weight, 0);

function FundCard({ fund, loading, onOpen }) {
  const pending = isPending(fund, loading);
  const holdings = fund.holdings || [];
  // The whole card opens the full details, by mouse or by keyboard (Enter or Space).
  const open = () => onOpen(fund.scheme_code);
  return <article
    className="fa-card fa-fund fa-fund--clickable"
    style={{ borderTopColor: fund.color }}
    role="button"
    tabIndex={0}
    aria-label={`Open full details for ${fund.scheme_name}`}
    onClick={open}
    onKeyDown={(event) => { if (event.target === event.currentTarget && (event.key === 'Enter' || event.key === ' ')) { event.preventDefault(); open(); } }}
  >
    <header className="fa-fund__head">
      <div><h3 title={fund.scheme_name}>{shortName(fund.scheme_name)}</h3><p>{fund.amc || 'AMFI'} · {fund.category || 'Category unavailable'}</p></div>
      <RiskBadge fund={fund} pending={pending} />
    </header>
    <dl className="fa-fund__metrics">
      <div><dt>NAV</dt><dd>{nav(fund.nav)}</dd></div>
      <div><dt>1Y</dt><dd className={upDown(fund.metrics?.r1y)}>{signedPct(fund.metrics?.r1y)}</dd></div>
      <div><dt>EXPENSE</dt><dd>{expense(fund, pending)}</dd></div>
      <div><dt>AUM</dt><dd>{aum(fund, pending)}</dd></div>
    </dl>
    <div className="fa-fund__section">
      <p className="fa-fund__details-head"><span>TOP HOLDINGS</span>{holdings.length > 0 && <small>{topShare(holdings, CARD_HOLDINGS).toFixed(1)}% in top {Math.min(CARD_HOLDINGS, holdings.length)}</small>}</p>
      <Holdings holdings={holdings} pending={pending} limit={CARD_HOLDINGS} />
    </div>
    <footer className="fa-fund__foot">
      <span>{sourceDates(fund) || (pending ? '' : 'No official disclosure found')}</span>
      <button type="button" onClick={(event) => { event.stopPropagation(); open(); }}>View full details <ArrowRight size={12} /></button>
    </footer>
  </article>;
}

function Allocation({ allocation, assetClass }) {
  const debtParts = [
    { key: 'debt_securities', label: 'Debt securities', className: assetClass === 'hybrid' ? 'fa-detail__part--foreign' : 'fa-detail__part--indian' },
    { key: 'other', label: 'Fund units, repo, cash & other', className: 'fa-detail__part--other' },
  ];
  const fofParts = [
    { key: 'fund_units', label: 'Underlying fund units', className: 'fa-detail__part--indian' },
    { key: 'other', label: 'Repo, cash & other', className: 'fa-detail__part--other' },
  ];
  const parts = assetClass === 'fof' ? fofParts : assetClass === 'hybrid' ? [{ key: 'equity', label: 'Equity', className: 'fa-detail__part--indian' }, ...debtParts] : assetClass === 'debt' ? debtParts : [
    { key: 'indian_equity', label: 'Indian equity', className: 'fa-detail__part--indian' },
    { key: 'international_equity', label: 'International equity', className: 'fa-detail__part--foreign' },
    { key: 'other', label: 'Debt, cash & other', className: 'fa-detail__part--other' },
  ];
  return <>
    <div className="fa-detail__bar" role="img" aria-label={parts.map((part) => `${part.label} ${allocation[part.key].toFixed(2)}%`).join(', ')}>
      {parts.map((part) => <i key={part.key} className={part.className} style={{ width: `${Math.max(0, allocation[part.key])}%` }} />)}
    </div>
    <ul className="fa-detail__legend">
      {parts.map((part) => <li key={part.key}><i className={part.className} />{part.label} <strong>{allocation[part.key].toFixed(2)}%</strong></li>)}
    </ul>
  </>;
}

function Sectors({ sectors }) {
  const largest = Math.max(1, ...sectors.map((sector) => sector.weight));
  return <ul className="fa-detail__sectors">
    {sectors.map((sector) => <li key={sector.name}>
      <span title={sector.name}>{sector.name}</span>
      <i><b className={sector.name === 'Others' ? 'fa-detail__sector--others' : ''} style={{ width: `${Math.max(0, sector.weight / largest) * 100}%` }} /></i>
      <strong>{sector.weight.toFixed(2)}%</strong>
    </li>)}
  </ul>;
}

function FundDetail({ fund, loading }) {
  const [showAll, setShowAll] = useState(false);
  const pending = isPending(fund, loading);
  const holdings = fund.holdings || [];
  const metrics = fund.metrics || {};
  const isDebt = fund.asset_class === 'debt' || fund.asset_class === 'hybrid' || fund.asset_class === 'fof';
  const shown = showAll ? holdings.length : Math.min(DETAIL_HOLDINGS, holdings.length);
  const tags = [fund.category?.split(' - ').pop(), [fund.plan, fund.option].filter(Boolean).join(' · '), fund.scheme_type && `${fund.scheme_type} ended`].filter(Boolean);
  const facts = [
    { label: 'AUM', value: aum(fund, pending) },
    { label: 'EXPENSE RATIO', value: expense(fund, pending) },
    { label: 'VOLATILITY (3Y)', value: metrics.volatility == null ? '—' : `${(metrics.volatility * 100).toFixed(2)}%` },
    { label: 'MAX DRAWDOWN (3Y)', value: signedPct(metrics.maxDrawdown), className: upDown(metrics.maxDrawdown) },
    { label: 'BENCHMARK', value: fund.benchmark || (pending ? '…' : '—') },
    { label: '3Y CAGR', value: signedPct(metrics.r3y), className: upDown(metrics.r3y) },
    { label: '5Y CAGR', value: signedPct(metrics.r5y), className: upDown(metrics.r5y) },
    { label: 'SHARPE RATIO', value: metrics.sharpe == null ? '—' : metrics.sharpe.toFixed(2) },
  ];
  return <article className="fa-card fa-detail">
    <header className="fa-detail__head">
      <div>
        <p className="fa-detail__amc">{fund.amc || 'AMFI'}</p>
        <h3 title={fund.scheme_name}>{shortName(fund.scheme_name)}</h3>
        <ul className="fa-detail__tags">{tags.map((tag) => <li key={tag}>{tag}</li>)}</ul>
      </div>
      <div className="fa-detail__nav">
        <small>NAV{fund.nav_date ? ` · ${fund.nav_date}` : ''}</small>
        <strong>{nav(fund.nav)}</strong>
        <small className={upDown(metrics.r1y)}>{signedPct(metrics.r1y)} in 1Y</small>
      </div>
    </header>
    <dl className="fa-detail__facts">
      {facts.map((fact) => <div key={fact.label}><dt>{fact.label}</dt><dd className={fact.className} title={fact.value}>{fact.value}</dd></div>)}
    </dl>
    <div className="fa-detail__body">
      <section className="fa-detail__panel">
        <h4><ListOrdered size={14} /> {showAll ? `ALL ${holdings.length} HOLDINGS` : `TOP ${DETAIL_HOLDINGS} HOLDINGS`}{holdings.length > 0 && <small>{topShare(holdings, shown).toFixed(1)}% of portfolio</small>}</h4>
        <Holdings holdings={holdings} pending={pending} limit={showAll ? null : DETAIL_HOLDINGS} showDetails={isDebt} />
        {holdings.length > DETAIL_HOLDINGS && <button type="button" className="fa-detail__more" onClick={() => setShowAll(!showAll)}>{showAll ? `Show top ${DETAIL_HOLDINGS}` : `Show all ${holdings.length} holdings`}</button>}
      </section>
      <section className="fa-detail__panel">
        <h4><PieChart size={14} /> ASSET ALLOCATION</h4>
        {fund.allocation ? <Allocation allocation={fund.allocation} assetClass={fund.asset_class} /> : <p className="fa-fund__empty">{pending ? 'Loading allocation…' : 'Allocation is not available for this fund.'}</p>}
        <h4 className="fa-detail__subhead"><BarChart3 size={14} /> {isDebt ? 'INSTRUMENT ALLOCATION' : 'SECTOR ALLOCATION'}</h4>
        {fund.sectors?.length ? <Sectors sectors={fund.sectors} /> : <p className="fa-fund__empty">{pending ? 'Loading sectors…' : 'Sector split is not available for this fund.'}</p>}
      </section>
    </div>
    <footer className="fa-detail__foot">
      <span>Scheme code {fund.scheme_code}{fund.isin ? ` · ISIN ${fund.isin}` : ''}</span>
      <span>{sourceDates(fund)}</span>
    </footer>
  </article>;
}

export default function FundamentalsTab({ funds, loading, error }) {
  const [openCode, setOpenCode] = useState(null);
  const openFund = funds.find((fund) => fund.scheme_code === openCode);
  return <>
    {error && <p className="fa-notice" role="alert"><Info size={14} /> <span>{error}</span></p>}
    <p className="fa-notice"><Info size={14} /> <span>Expense ratio and AUM are official AMFI disclosures. Holdings, sectors and allocation come from each AMC's monthly portfolio file.</span></p>
    {openFund
      ? <>
        <button type="button" className="fa__action fa-detail__back" onClick={() => setOpenCode(null)}><ChevronLeft size={14} /> All funds</button>
        <div className="fa-detail__layout">
          <nav className="fa-card fa-detail__funds" aria-label="Selected funds">
            {funds.map((fund) => <button key={fund.scheme_code} type="button" aria-current={fund.scheme_code === openCode} className={fund.scheme_code === openCode ? 'is-active' : ''} onClick={() => setOpenCode(fund.scheme_code)}>
              <i style={{ background: fund.color }} /><span title={fund.scheme_name}>{shortName(fund.scheme_name)}</span>
            </button>)}
          </nav>
          <FundDetail key={openFund.scheme_code} fund={openFund} loading={loading} />
        </div>
      </>
      : <div className="fa-funds">{funds.map((fund) => <FundCard key={fund.scheme_code} fund={fund} loading={loading} onOpen={setOpenCode} />)}</div>}
  </>;
}
