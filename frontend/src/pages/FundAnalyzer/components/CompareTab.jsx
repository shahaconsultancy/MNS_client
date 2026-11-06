import { useState } from 'react';
import { ArrowDown, ArrowUp } from 'lucide-react';
import { nav, signedPct } from '../fundAnalysis.js';

const COLUMNS = [
  { key: 'nav', label: 'NAV', format: nav },
  { key: 'r1m', label: '1M', format: signedPct, better: 'high', colored: true }, { key: 'r3m', label: '3M', format: signedPct, better: 'high', colored: true },
  { key: 'r6m', label: '6M', format: signedPct, better: 'high', colored: true }, { key: 'r1y', label: '1Y', format: signedPct, better: 'high', colored: true },
  { key: 'r3y', label: '3Y CAGR', format: signedPct, better: 'high', colored: true }, { key: 'r5y', label: '5Y CAGR', format: signedPct, better: 'high', colored: true },
  { key: 'volatility', label: 'VOLATILITY', format: (n) => n == null ? '—' : `${(n * 100).toFixed(2)}%`, better: 'low' },
  { key: 'sharpe', label: 'SHARPE', format: (n) => n == null ? '—' : n.toFixed(2), better: 'high' },
  { key: 'maxDrawdown', label: 'MAX DD', format: signedPct, better: 'high', colored: true }, { key: 'ter', label: 'TER', format: (n) => n == null ? '—' : `${Number(n).toFixed(2)}%`, better: 'low' },
];

export default function CompareTab({ funds, riskFreeRate }) {
  const [sort, setSort] = useState(null);
  const handleSort = (key) => setSort(sort?.key !== key ? { key, direction: -1 } : sort.direction === -1 ? { key, direction: 1 } : null);
  const value = (fund, key) => key in fund ? fund[key] : fund.metrics?.[key];
  const rows = sort ? [...funds].sort((a, b) => ((value(a, sort.key) ?? 0) - (value(b, sort.key) ?? 0)) * sort.direction) : funds;
  const bestValue = (column) => { if (!column.better) return null; const values = funds.map((fund) => value(fund, column.key)).filter((item) => item != null); return !values.length ? null : column.better === 'low' ? Math.min(...values) : Math.max(...values); };
  return <section className="fa-card fa-compare"><div className="fa-section-head"><h2>Returns &amp; risk comparison</h2><p>Click a column to sort. Gold outline marks the best fund in each column.</p></div><div className="fa-table-wrap"><table className="fa-table"><thead><tr className="fa-table__groups"><th /><th /><th colSpan={6}>TRAILING RETURNS</th><th colSpan={3}>RISK (3Y)</th><th>COST</th></tr><tr><th className="fa-table__fund">FUND</th>{COLUMNS.map((column) => <th key={column.key}><button type="button" onClick={() => handleSort(column.key)}>{column.label}{sort?.key === column.key && (sort.direction === 1 ? <ArrowUp size={11} /> : <ArrowDown size={11} />)}</button></th>)}</tr></thead><tbody>{rows.map((fund) => <tr key={fund.scheme_code}><td className="fa-table__fund"><div><i style={{ background: fund.color }} /><div><strong>{fund.scheme_name}</strong><span><em>{fund.category || 'Category unavailable'}</em> {fund.amc || 'AMFI'}</span></div></div></td>{COLUMNS.map((column) => { const current = value(fund, column.key); const best = bestValue(column); const color = column.colored ? (current >= 0 ? ' fa-up' : ' fa-down') : ''; return <td key={column.key}><span className={`fa-table__cell${current != null && current === best ? ' fa-table__cell--best' : ''}${color}`}>{column.format(current)}</span></td>; })}</tr>)}</tbody></table></div><footer className="fa-compare__foot"><span>Returns above 1Y are annualised (CAGR). Risk metrics use 3Y daily NAV · risk-free rate {(riskFreeRate * 100).toFixed(2)}%. TER is the latest AMFI-disclosed total.</span><span className="fa-compare__legend"><i /> Best in column</span></footer></section>;
}
