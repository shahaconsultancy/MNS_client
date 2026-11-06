import { useEffect, useRef, useState } from 'react';
import { LineChart, Search, Trash2, X } from 'lucide-react';
import { fundColor } from '../fundData.js';
import { searchFunds } from '../../../services/fundAnalyzerService.js';

export default function FundPicker({ selectedFunds, onChange, onClear, onUpdate, loading }) {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState([]);
  const [open, setOpen] = useState(false);
  const [searching, setSearching] = useState(false);
  const inputRef = useRef(null);

  useEffect(() => {
    const timer = setTimeout(async () => {
      if (query.trim().length < 2) { setResults([]); return; }
      setSearching(true);
      try { setResults((await searchFunds(query.trim())).results || []); } catch { setResults([]); } finally { setSearching(false); }
    }, 350);
    return () => clearTimeout(timer);
  }, [query]);

  const addFund = (fund) => {
    if (selectedFunds.some((item) => item.scheme_code === fund.scheme_code)) return;
    onChange([...selectedFunds, fund]);
    setQuery('');
  };

  return (
    <section className="fa-card fa-picker">
      <div className="fa-picker__head">
        <div><h2>Select funds to compare</h2><p>Search AMFI schemes by name, scheme code or ISIN. Direct + Growth is preferred.</p></div>
        <button type="button" className="fa-picker__clear" onClick={onClear}><Trash2 size={14} /> Clear all</button>
      </div>
      <div className="fa-search">
        <label className="fa-search__box">
          <Search size={16} /><input ref={inputRef} type="text" placeholder="Search a mutual fund…" value={query} onChange={(e) => { setQuery(e.target.value); setOpen(true); }} onFocus={() => setOpen(true)} /><kbd>/</kbd>
        </label>
        {open && query.trim().length >= 2 && <div className="fa-search__menu">
          <p className="fa-search__title">{searching ? 'SEARCHING AMFI…' : `${results.length} RESULTS`}</p>
          {!searching && results.length === 0 && <p className="fa-search__none">No AMFI schemes match this search.</p>}
          {results.map((fund) => {
            const added = selectedFunds.some((item) => item.scheme_code === fund.scheme_code);
            return <button key={`${fund.scheme_code}-${fund.isin || ''}`} type="button" className="fa-search__row" disabled={added} onMouseDown={(e) => e.preventDefault()} onClick={() => addFund(fund)}>
              <span><strong>{fund.scheme_name}</strong><small>{fund.amc || 'AMFI'} · {fund.category || 'Category unavailable'} · {fund.scheme_code}{added ? ' · Added' : ''}</small></span>
              <span className="fa-search__cagr"><strong>{fund.nav == null ? '—' : `₹${fund.nav}`}</strong><small>LIVE NAV</small></span>
            </button>;
          })}
        </div>}
      </div>
      {selectedFunds.length > 0 && <ul className="fa-chips">{selectedFunds.map((fund, i) => <li key={fund.scheme_code} className="fa-chip"><i style={{ background: fundColor(i) }} /><strong>{fund.scheme_name}</strong><small>{fund.scheme_code}</small><button type="button" aria-label={`Remove ${fund.scheme_name}`} onClick={() => onChange(selectedFunds.filter((item) => item.scheme_code !== fund.scheme_code))}><X size={13} /></button></li>)}</ul>}
      <div className="fa-picker__foot"><div className="fa-picker__count">{selectedFunds.length} {selectedFunds.length === 1 ? 'fund' : 'funds'} selected</div><button type="button" className="fa-picker__update" disabled={!selectedFunds.length || loading} onClick={onUpdate}><LineChart size={15} /> {loading ? 'Loading live data…' : 'Update analysis'}</button></div>
    </section>
  );
}
