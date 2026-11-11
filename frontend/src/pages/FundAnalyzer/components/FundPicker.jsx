import { useEffect, useRef, useState } from 'react';
import { LineChart, Loader2, Search, Trash2, X } from 'lucide-react';
import { fundColor } from '../fundData.js';
import { searchFunds } from '../../../services/fundAnalyzerService.js';

// AMFI lists plan and option in their own columns, so the bare scheme name is the same for every variant.
const fundLabel = (fund) => fund.display_name || [fund.scheme_name, fund.plan, fund.option].filter(Boolean).join(' - ');
const PAGE_SIZE = 50;
const navCaption = (fund) => {
  if (fund.nav == null) return 'NAV NOT PUBLISHED';
  if (fund.scheme_status && fund.scheme_status !== 'ACTIVE') return `${fund.scheme_status} · LAST NAV ${fund.nav_date || ''}`;
  return fund.nav_date ? `NAV ${fund.nav_date}` : 'NAV';
};
const when = (iso) => (iso ? new Date(iso).toLocaleString('en-GB', { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' }) : null);

export default function FundPicker({ selectedFunds, onChange, onClear, onUpdate, loading }) {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState([]);
  const [total, setTotal] = useState(0);
  const [open, setOpen] = useState(false);
  const [searching, setSearching] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [failed, setFailed] = useState(false);
  const [registry, setRegistry] = useState(null);
  const inputRef = useRef(null);
  // Only the newest request may change the list, so a slow earlier reply cannot overwrite it.
  const requestId = useRef(0);

  useEffect(() => {
    const timer = setTimeout(async () => {
      const id = ++requestId.current;
      if (query.trim().length < 2) { setResults([]); setTotal(0); setFailed(false); return; }
      setSearching(true);
      try {
        const data = await searchFunds(query.trim(), { limit: PAGE_SIZE });
        if (id !== requestId.current) return;
        const rows = data.results || [];
        setResults(rows); setTotal(data.total ?? rows.length); setRegistry(data.registry || null); setFailed(false);
      } catch {
        if (id === requestId.current) { setResults([]); setTotal(0); setFailed(true); }
      } finally {
        if (id === requestId.current) setSearching(false);
      }
    }, 350);
    return () => clearTimeout(timer);
  }, [query]);

  const loadMore = async () => {
    const id = requestId.current;
    setLoadingMore(true);
    try {
      const data = await searchFunds(query.trim(), { offset: results.length, limit: PAGE_SIZE });
      if (id !== requestId.current) return;
      setResults((current) => [...current, ...(data.results || []).filter((row) => !current.some((item) => item.scheme_code === row.scheme_code))]);
      setTotal(data.total ?? total);
    } catch {
      // The rows already shown stay; the button remains so the user can try again.
    } finally {
      setLoadingMore(false);
    }
  };

  const addFund = (fund) => {
    if (selectedFunds.some((item) => item.scheme_code === fund.scheme_code)) return;
    onChange([...selectedFunds, fund]);
    setQuery('');
  };

  return (
    <section className="fa-card fa-picker">
      <div className="fa-picker__head">
        <div><h2>Select funds to compare</h2><p>Search AMFI schemes by name, scheme code or ISIN. Direct and Regular plans, Growth and IDCW options are all listed; Direct Growth comes first.</p></div>
        <button type="button" className="fa-picker__clear" onClick={onClear}><Trash2 size={14} /> Clear all</button>
      </div>
      <div className="fa-search">
        <label className="fa-search__box">
          <Search size={16} /><input ref={inputRef} type="text" placeholder="Search a mutual fund…" value={query} onChange={(e) => { setQuery(e.target.value); setOpen(true); }} onFocus={() => setOpen(true)} /><kbd>/</kbd>
        </label>
        {open && query.trim().length >= 2 && <div className="fa-search__menu">
          <p className="fa-search__title">{searching ? 'SEARCHING AMFI…' : failed ? 'SEARCH UNAVAILABLE' : total > results.length ? `SHOWING ${results.length} OF ${total} RESULTS` : `${total} RESULTS`}</p>
          {!searching && failed && <p className="fa-search__none">Fund search is temporarily unavailable. Please try again shortly.</p>}
          {!searching && !failed && results.length === 0 && <p className="fa-search__none">No AMFI schemes match this search.</p>}
          {results.map((fund) => {
            const added = selectedFunds.some((item) => item.scheme_code === fund.scheme_code);
            return <button key={`${fund.scheme_code}-${fund.isin || ''}`} type="button" className="fa-search__row" disabled={added} onMouseDown={(e) => e.preventDefault()} onClick={() => addFund(fund)}>
              <span><strong>{fundLabel(fund)}</strong><small>{fund.amc || 'AMFI'} · {fund.category || 'Category unavailable'} · {fund.scheme_code}{fund.isin ? ` · ${fund.isin}` : ''}{added ? ' · Added' : ''}</small></span>
              <span className="fa-search__cagr"><strong>{fund.nav == null ? '—' : `₹${fund.nav}`}</strong><small>{navCaption(fund)}</small></span>
            </button>;
          })}
          {!searching && !failed && total > results.length && <button type="button" className="fa-search__more" disabled={loadingMore} onMouseDown={(e) => e.preventDefault()} onClick={loadMore}>{loadingMore ? 'Loading…' : `Show ${Math.min(PAGE_SIZE, total - results.length)} more of ${total - results.length} remaining`}</button>}
          {!searching && registry?.updatedAt && <p className="fa-search__source">AMFI scheme list updated {when(registry.updatedAt)}{registry.stale ? ' · AMFI could not be reached since, so this is the last good list' : ''}</p>}
        </div>}
      </div>
      {selectedFunds.length > 0 && <ul className="fa-chips">{selectedFunds.map((fund, i) => <li key={fund.scheme_code} className="fa-chip"><i style={{ background: fundColor(i) }} /><strong>{fundLabel(fund)}</strong><small>{fund.scheme_code}</small><button type="button" aria-label={`Remove ${fundLabel(fund)}`} onClick={() => onChange(selectedFunds.filter((item) => item.scheme_code !== fund.scheme_code))}><X size={13} /></button></li>)}</ul>}
      <div className="fa-picker__foot"><div className="fa-picker__count">{selectedFunds.length} {selectedFunds.length === 1 ? 'fund' : 'funds'} selected</div><button type="button" className={`fa-picker__update${loading ? ' fa-picker__update--loading' : !selectedFunds.length ? ' fa-picker__update--empty' : ''}`} disabled={!selectedFunds.length || loading} title={!selectedFunds.length && !loading ? 'Select at least one fund to compare' : undefined} onClick={onUpdate}>{loading ? <Loader2 size={15} /> : <LineChart size={15} />} {loading ? 'Loading live data…' : 'Update analysis'}</button></div>
    </section>
  );
}
