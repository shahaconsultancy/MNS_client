import { useState } from 'react';
import { Info } from 'lucide-react';

const SCALE_MAX = 60;
const TOP_PAIRS = 5;
const STOCK_CHIPS = 4;
// More funds than this and the matrix is laid out full width, with the pair details below it.
const STACK_AFTER_FUNDS = 5;
// More funds than this and the matrix numbers its columns, so it stays narrow enough to read (and to fit a PDF page).
const COMPACT_AFTER_FUNDS = 10;

const shortName = (name) => name.split(' - ')[0].replace(/ Fund$/i, '');
const pairKey = (pair) => `${pair.left.scheme_code}-${pair.right.scheme_code}`;
const cellColor = (value) => `rgba(194, 144, 53, ${0.12 + 0.88 * Math.min(value / SCALE_MAX, 1)})`;
const overlapLevel = (value) => (value < 25 ? 'Low overlap' : value <= 50 ? 'Moderate overlap' : 'High overlap');
const pct = (value) => (value == null ? '—' : `${value.toFixed(2)}%`);
const count = (value) => (value == null ? '—' : value);
const SOURCE_LABEL = { official_amc: 'Official AMC', charlie_alpha: 'Third-party API', mixed: 'Official AMC and Third-party API' };
const classLabel = { equity: 'Equity', debt: 'Debt', fund_unit: 'Fund unit' };
const dateLabel = (date) => (date
  ? new Date(`${date}T00:00:00Z`).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' })
  : null);
const monthLabel = (month) => (month
  ? new Date(`${month}-01T00:00:00Z`).toLocaleString('en-US', { month: 'long', year: 'numeric', timeZone: 'UTC' })
  : null);

function Matrix({ funds, pairs, selected, onSelect }) {
  const pairFor = (a, b) => pairs.find((pair) => (pair.left.scheme_code === a && pair.right.scheme_code === b) || (pair.left.scheme_code === b && pair.right.scheme_code === a));
  // Many funds: columns carry the fund's number (its row label shows the number and name), not the full name.
  const compact = funds.length > COMPACT_AFTER_FUNDS;
  return <section className={`fa-card fa-matrix${compact ? ' fa-matrix--compact' : ''}`}>
    <div className="fa-section-head fa-section-head--flush"><h2>Overlap matrix</h2><p>% of portfolio weight the two funds hold in common. Click any cell for details.{compact && ' Columns are numbered as the rows; hover a number for the fund name.'} A dash means no figure, not 0%.</p></div>
    <div className="fa-matrix__scroll">
      <div className="fa-matrix__grid" style={{ gridTemplateColumns: `var(--fa-matrix-label, 130px) repeat(${funds.length}, minmax(var(--fa-matrix-col, 76px), 1fr))` }}>
        <span />
        {funds.map((fund, index) => <span key={fund.scheme_code} className="fa-matrix__col" title={fund.scheme_name}><i style={{ background: fund.color }} />{compact ? index + 1 : shortName(fund.scheme_name)}</span>)}
        {funds.map((rowFund, index) => [
          <span key={`${rowFund.scheme_code}-label`} className="fa-matrix__row" title={rowFund.scheme_name}>{compact && <b>{index + 1}</b>}{shortName(rowFund.scheme_name)} <i style={{ background: rowFund.color }} /></span>,
          ...funds.map((colFund) => {
            const key = `${rowFund.scheme_code}-${colFund.scheme_code}`;
            const pair = rowFund.scheme_code === colFund.scheme_code ? null : pairFor(rowFund.scheme_code, colFund.scheme_code);
            if (!pair) return <span key={key} className="fa-matrix__self">—</span>;
            const active = selected && pairKey(selected) === pairKey(pair);
            return <button key={key} type="button" className={`fa-matrix__cell${active ? ' fa-matrix__cell--active' : ''}`} style={{ background: cellColor(pair.overlap_pct) }} title={`${monthLabel(pair.portfolio_month) || ''}`} aria-pressed={active} onClick={() => onSelect(pairKey(pair))}>{pair.overlap_pct.toFixed(0)}%</button>;
          }),
        ])}
      </div>
    </div>
    <div className="fa-matrix__legend">0% <i /> {SCALE_MAX}%+</div>
  </section>;
}

function PairDetail({ pair, colorOf }) {
  const stocks = pair.common_holdings;
  const withUnits = pair.including_fund_units;
  const isFof = pair.overlap_type === 'DIRECT_FOF';
  const largest = Math.max(1, ...stocks.flatMap((stock) => [stock.left_weight, stock.right_weight]));
  const bar = (weight, code) => <span>
    <i style={{ width: `${Math.max(3, (weight / largest) * 60)}px`, background: colorOf(code) }} />{weight.toFixed(2)}%
  </span>;
  return <section className="fa-card fa-pair">
    <div className="fa-pair__head">
      <div>
        <p><i style={{ background: colorOf(pair.left.scheme_code) }} />{pair.left.scheme_name}</p>
        <p><i style={{ background: colorOf(pair.right.scheme_code) }} />{pair.right.scheme_name}</p>
        <span className="fa-pair__level">{overlapLevel(pair.overlap_pct)}</span>
      </div>
      <div className="fa-pair__total"><strong>{pair.overlap_pct.toFixed(2)}%</strong><span>{isFof ? 'DIRECT FOF OVERLAP' : 'OVERLAP'}</span><small>{isFof ? (dateLabel(pair.portfolio_date) || monthLabel(pair.portfolio_month)) : monthLabel(pair.portfolio_month)}</small></div>
    </div>
    {isFof
      ? <dl className="fa-pair__split">
        <div><dt>DIRECT FOF OVERLAP</dt><dd>{pct(pair.overlap_pct)}</dd><small>of each fund's NAV, at most</small></div>
        <div><dt>COMMON UNDERLYING FUNDS</dt><dd>{pair.common_fund_unit_count}</dd><small>identical fund-unit ISINs</small></div>
        <div><dt>PORTFOLIO DATE</dt><dd>{dateLabel(pair.portfolio_date) || '—'}</dd><small>{pair.portfolio_date ? 'same date for both funds' : 'funds report on different dates'}</small></div>
      </dl>
      : <dl className="fa-pair__split">
        <div><dt>EQUITY</dt><dd>{pct(pair.equity_overlap_pct)}</dd><small>{count(pair.common_equity_count)} common</small></div>
        <div><dt>DEBT</dt><dd>{pct(pair.debt_overlap_pct)}</dd><small>{count(pair.common_debt_count)} common</small></div>
        <div><dt>COMBINED</dt><dd>{pct(pair.overlap_pct)}</dd><small>{pair.common_count} common</small></div>
      </dl>}
    {pair.holdings_source && <p className="fa-pair__units" data-testid="holdings-source">Holdings source: {SOURCE_LABEL[pair.holdings_source] || pair.holdings_source}{pair.holdings_source === 'mixed' && ` (${shortName(pair.left.scheme_name)}: ${SOURCE_LABEL[pair.left_source]}; ${shortName(pair.right.scheme_name)}: ${SOURCE_LABEL[pair.right_source]})`}{pair.holdings_source !== 'official_amc' && '. Third-party holdings come from an independent provider, not from the AMC or AMFI.'}</p>}
    {isFof && <p className="fa-pair__units">Direct FoF Overlap compares the mutual fund units the two schemes hold. It is not common stock or bond exposure inside those funds.</p>}
    {withUnits && <p className="fa-pair__units">Including fund units: <strong>{withUnits.overlap_pct.toFixed(2)}%</strong>. Adds {withUnits.fund_units.map((unit) => `${unit.name} (${unit.left_weight.toFixed(2)}% and ${unit.right_weight.toFixed(2)}%)`).join(', ')}. The overlap above covers direct debt securities only.</p>}
    {stocks.length === 0
      ? <p className="fa-pair__none">{pair.message || 'No common securities'}</p>
      : <>
        <div className="fa-pair__columns"><span>{isFof ? 'COMMON UNDERLYING FUNDS' : 'COMMON HOLDINGS'} ({stocks.length})</span><span>WEIGHT IN EACH</span></div>
        <div className="fa-pair__list">
          {stocks.map((stock) => <div key={stock.isin} className="fa-pair__row">
            <span title={stock.isin}>{stock.name}<small className="fa-pair__meta">{classLabel[stock.security_class]} · adds {stock.overlap_weight.toFixed(2)}%</small></span>
            <div className="fa-pair__bars">{bar(stock.left_weight, pair.left.scheme_code)}{bar(stock.right_weight, pair.right.scheme_code)}</div>
          </div>)}
        </div>
      </>}
  </section>;
}

function TopPairs({ pairs, selected, onSelect, colorOf, showMonth }) {
  return <section className="fa-card fa-pairs">
    <div className="fa-section-head fa-section-head--flush"><h2>Highest overlapping pairs</h2><p>Funds that share the most portfolio weight, with the securities they hold in common.</p></div>
    {pairs.slice(0, TOP_PAIRS).map((pair, index) => {
      const hidden = pair.common_holdings.length - STOCK_CHIPS;
      return <button key={pairKey(pair)} type="button" className={`fa-pairs__row${selected && pairKey(selected) === pairKey(pair) ? ' fa-pairs__row--active' : ''}`} onClick={() => onSelect(pairKey(pair))}>
        <span className="fa-pairs__rank">{index + 1}</span>
        <span className="fa-pairs__body">
          <span className="fa-pairs__names"><i style={{ background: colorOf(pair.left.scheme_code) }} />{pair.left.scheme_name}<em>×</em><i style={{ background: colorOf(pair.right.scheme_code) }} />{pair.right.scheme_name}</span>
          <span className="fa-pairs__stocks">
            {pair.common_holdings.slice(0, STOCK_CHIPS).map((stock) => <small key={stock.isin}>{stock.name}</small>)}
            {hidden > 0 && <small>+{hidden} more</small>}
          </span>
        </span>
        <span className="fa-pairs__badge">{pair.overlap_pct.toFixed(2)}% overlap{showMonth && <small> · {monthLabel(pair.portfolio_month)}</small>}</span>
      </button>;
    })}
  </section>;
}

const HISTORICAL = 'HISTORICAL_HOLDINGS_UNAVAILABLE';
const today = new Date().toISOString().slice(0, 7);

// Past months can only be compared from verified saved portfolios, one chosen month for every fund.
function HistoricalPicker({ overlap, onSelect }) {
  const [month, setMonth] = useState(overlap?.requested_month || '');
  const historicalFunds = (overlap?.funds || []).filter((fund) => fund.reason_code === HISTORICAL || (fund.scheme_status && fund.scheme_status !== 'ACTIVE'));
  const saved = [...new Set(historicalFunds.flatMap((fund) => fund.historical_months_available || []))];
  if (!onSelect || (!historicalFunds.length && overlap?.mode !== 'historical')) return null;
  return <form className="fa-notice" onSubmit={(event) => { event.preventDefault(); if (month) onSelect(month); }}>
    <Info size={15} />
    <span>
      {overlap?.mode === 'historical'
        ? `Historical comparison for ${monthLabel(overlap.requested_month)}: only portfolios reported in that month are used. `
        : 'Matured and closed schemes have no current portfolio. '}
      Choose a past reporting month to compare saved, verified portfolios from that same month.{' '}
      <input type="month" max={today} value={month} onChange={(event) => setMonth(event.target.value)} aria-label="Historical reporting month" />{' '}
      <button type="submit" disabled={!month}>Compare month</button>
      {overlap?.mode === 'historical' && <> <button type="button" onClick={() => onSelect(undefined)}>Back to latest</button></>}
      {saved.length > 0 && <> Saved: {saved.map((item) => <button key={item} type="button" onClick={() => { setMonth(item); onSelect(item); }}>{monthLabel(item)}</button>)}</>}
    </span>
  </form>;
}

// No figure at all. Say which fund or pair is the problem, never a 0%.
function Unavailable({ overlap, onHistoricalMonth }) {
  const problems = (overlap?.funds || []).filter((fund) => !fund.available && fund.scheme_name);
  const gaps = overlap?.pair_reasons || [];
  const gapNames = (gap) => [gap.left, gap.right].map((code) => (overlap.funds || []).find((fund) => fund.scheme_code === code)?.scheme_name || code).join(' and ');
  return <section className="fa-card fa__empty">
    <p><strong>{overlap?.highest_overlap?.message || 'Portfolio holdings unavailable'}</strong></p>
    {problems.map((fund) => <p key={fund.scheme_code}>{fund.message || `${fund.scheme_name}: ${fund.status_message || fund.reason}`}{fund.scheme_status && fund.scheme_status !== 'ACTIVE' && fund.last_nav_date ? ` This scheme is ${fund.scheme_status.toLowerCase()}; its last NAV was on ${dateLabel(fund.last_nav_date)}.` : ''}</p>)}
    {gaps.filter((gap) => gap.reason_code === 'NO_COMMON_REPORTING_MONTH').map((gap) => <p key={`${gap.left}-${gap.right}`}>No common reporting month for {gapNames(gap)} ({monthLabel(gap.left_portfolio_month)} and {monthLabel(gap.right_portfolio_month)}).</p>)}
    <HistoricalPicker overlap={overlap} onSelect={onHistoricalMonth} />
  </section>;
}

// The server's reason text is kept as it is; only the long Fund of Funds sentence gets a short label for table rows.
const SHORT_REASON = { FOF_LOOKTHROUGH_UNAVAILABLE: 'Fund of Funds look-through unavailable' };
const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

// The pairs without a figure, one group per distinct reason, the largest first. Never shown as 0%.
function groupGaps(gaps) {
  const groups = new Map();
  gaps.forEach((gap) => {
    const key = `${gap.reason_code}|${gap.message}`;
    if (!groups.has(key)) groups.set(key, { key, code: gap.reason_code, message: gap.message, label: SHORT_REASON[gap.reason_code] || gap.message, gaps: [] });
    groups.get(key).gaps.push(gap);
  });
  return [...groups.values()].sort((a, b) => b.gaps.length - a.gaps.length);
}

const gapReason = (gap) => (gap.reason_code === 'NO_COMMON_REPORTING_MONTH' && gap.left_portfolio_month && gap.right_portfolio_month
  ? `No common month: ${monthLabel(gap.left_portfolio_month)} vs ${monthLabel(gap.right_portfolio_month)}`
  : SHORT_REASON[gap.reason_code] || gap.message);

// One compact card for reporting month, coverage, sources and exclusions. Details open on request (always in the PDF).
function OverlapSummary({ overlap, pairs, funds, expandAll }) {
  const nameOf = (code) => funds.find((fund) => fund.scheme_code === code)?.scheme_name || (overlap.funds || []).find((fund) => fund.scheme_code === code)?.scheme_name || code;
  const results = overlap.funds || [];
  const gaps = overlap.pair_reasons || [];
  const groups = groupGaps(gaps);
  const total = overlap.total_pairs ?? pairs.length + gaps.length;
  const compared = overlap.compared_pairs ?? pairs.length;
  const excluded = Math.max(total - compared, 0);
  const coverage = total ? (compared / total) * 100 : 0;
  const coverageText = `${coverage % 1 === 0 ? coverage.toFixed(0) : coverage.toFixed(1)}%`;

  const monthCounts = [...pairs.reduce((map, pair) => map.set(pair.portfolio_month, (map.get(pair.portfolio_month) || 0) + 1), new Map())].sort((a, b) => b[0].localeCompare(a[0]));
  const mixed = overlap.pairs_different_months;
  const monthValue = mixed ? `Mixed · ${plural(monthCounts.length, 'month')}` : monthLabel(overlap.common_month || overlap.portfolio_month) || '—';

  const available = results.filter((fund) => fund.available);
  const skipped = results.filter((fund) => !fund.available);
  const unclassified = results.filter((fund) => fund.unclassified_pct);
  const sourceCounts = [...available.reduce((map, fund) => map.set(fund.holdings_source, (map.get(fund.holdings_source) || 0) + 1), new Map())];
  const thirdParty = sourceCounts.some(([source]) => source && source !== 'official_amc');
  const notEquityOnly = results.some((fund) => fund.asset_class && fund.asset_class !== 'equity');
  const sourceName = (fund) => SOURCE_LABEL[fund.holdings_source] || fund.holdings_source_label || fund.holdings_source || '—';

  return <section className="fa-card fa-summary" aria-labelledby="fa-summary-title">
    <div className="fa-summary__head">
      <h2 id="fa-summary-title">Portfolio overlap summary</h2>
      <p>Disclosed {notEquityOnly ? 'securities' : 'equity portfolios'}, matched by ISIN{overlap.mode === 'historical' ? `. Historical comparison for ${monthLabel(overlap.requested_month)}` : ''}.</p>
    </div>
    <dl className="fa-summary__stats">
      <div><dt>Reporting month</dt><dd>{monthValue}</dd></div>
      <div><dt>Possible pairs</dt><dd>{total}</dd></div>
      <div><dt>Compared</dt><dd>{compared}</dd></div>
      <div><dt>Not compared</dt><dd className={excluded ? 'fa-summary__warn' : undefined}>{excluded}</dd></div>
      <div><dt>Coverage</dt><dd>{coverageText}</dd></div>
    </dl>
    <div className="fa-summary__bar" role="img" aria-label={`${compared} of ${total} pairs compared (${coverageText})`}><b style={{ width: `${coverage}%` }} /></div>

    <ul className="fa-summary__notes">
      {mixed && <li><strong>Mixed reporting months.</strong> No month is common to all funds, so each pair uses the latest month its own two funds share; figures from different months are not directly comparable. Pairs by month: {monthCounts.map(([month, n]) => `${monthLabel(month)} (${n})`).join(', ')}.</li>}
      <li><strong>Holdings source:</strong> {sourceCounts.length ? sourceCounts.map(([source, n]) => `${SOURCE_LABEL[source] || source} for ${plural(n, 'fund')}`).join(', ') : '—'}.{thirdParty && ' Third-party holdings come from an independent provider, not from the AMC or AMFI.'}</li>
      {excluded > 0 && <li><strong>{plural(excluded, 'pair')} not compared</strong> and shown as a dash, never 0%: {groups.map((group) => `${group.label} (${group.gaps.length})`).join('; ')}.</li>}
      {skipped.length > 0 && <li><strong>{plural(skipped.length, 'fund')} without usable holdings:</strong> {skipped.length <= 3 ? skipped.map((fund) => `${fund.scheme_name || fund.scheme_code} (${fund.message || fund.status_message || fund.reason})`).join('; ') : 'see holdings sources below'}.</li>}
      {unclassified.length > 0 && <li><strong>Incomplete coverage:</strong> {unclassified.length <= 3 ? unclassified.map((fund) => `${fund.scheme_name} has ${fund.unclassified_pct.toFixed(2)}% of NAV we could not classify`).join('; ') : `${plural(unclassified.length, 'fund')} have part of NAV we could not classify; see holdings sources below`}.</li>}
    </ul>

    <div className="fa-summary__details">
      {excluded > 0 && <details open={expandAll || undefined}>
        <summary>View excluded comparisons ({gaps.length})</summary>
        {groups.map((group) => <div key={group.key} className="fa-summary__group">
          <p><strong>{group.label}</strong> · {plural(group.gaps.length, 'pair')}{group.message && group.message !== group.label && <span> — {group.message}</span>}</p>
          <table className="fa-summary__table">
            <thead><tr><th>Fund A</th><th>Fund B</th><th>Reason</th></tr></thead>
            <tbody>{group.gaps.map((gap) => <tr key={`${gap.left}-${gap.right}`}>
              <td data-label="Fund A">{nameOf(gap.left)}</td>
              <td data-label="Fund B">{nameOf(gap.right)}</td>
              <td data-label="Reason">{gapReason(gap)}</td>
            </tr>)}</tbody>
          </table>
        </div>)}
      </details>}
      <details open={expandAll || undefined}>
        <summary>View holdings sources ({results.length})</summary>
        <table className="fa-summary__table">
          <thead><tr><th>Fund</th><th>Reporting month</th><th>Source</th><th>Notes</th></tr></thead>
          <tbody>{results.map((fund) => <tr key={fund.scheme_code}>
            <td data-label="Fund">{fund.scheme_name || fund.scheme_code}</td>
            <td data-label="Reporting month">{monthLabel(fund.portfolio_month) || '—'}</td>
            <td data-label="Source">{fund.available ? sourceName(fund) : '—'}</td>
            <td data-label="Notes">{fund.available
              ? (fund.unclassified_pct ? `${fund.unclassified_pct.toFixed(2)}% of NAV not classified` : '—')
              : <span className="fa-summary__warn">{fund.message || fund.status_message || fund.reason || 'Holdings unavailable'}</span>}</td>
          </tr>)}</tbody>
        </table>
      </details>
    </div>
  </section>;
}

export default function OverlapTab({ funds, overlap, overlapLoading, overlapStage, onHistoricalMonth, expandAll }) {
  const [activeKey, setActiveKey] = useState(null);
  if (funds.length < 2) return <p className="fa-card fa__empty">Add at least two funds to compare their holdings.</p>;
  if (overlapLoading) return <p className="fa-card fa__empty" role="status" aria-live="polite">{overlapStage || 'Fetching portfolio holdings...'}</p>;

  const pairs = [...(overlap?.pairs || [])].sort((a, b) => b.overlap_pct - a.overlap_pct);
  if (!pairs.length) return <Unavailable overlap={overlap} onHistoricalMonth={onHistoricalMonth} />;

  const selected = pairs.find((pair) => pairKey(pair) === activeKey) || pairs[0];
  const colorOf = (code) => funds.find((fund) => fund.scheme_code === code)?.color;

  return <>
    <OverlapSummary overlap={overlap} pairs={pairs} funds={funds} expandAll={expandAll} />
    <HistoricalPicker overlap={overlap} onSelect={onHistoricalMonth} />
    {/* Beyond five funds the matrix needs the full width, so the pair details go below it. */}
    <div className={`fa-overlap${funds.length > STACK_AFTER_FUNDS ? ' fa-overlap--stacked' : ''}`}>
      <Matrix funds={funds} pairs={pairs} selected={selected} onSelect={setActiveKey} />
      <PairDetail pair={selected} colorOf={colorOf} />
    </div>
    <TopPairs pairs={pairs} selected={selected} onSelect={setActiveKey} colorOf={colorOf} showMonth={overlap.pairs_different_months} />
  </>;
}
