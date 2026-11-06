import { useEffect, useRef, useState } from 'react';
import { Blend, Download, List, PieChart } from 'lucide-react';
import FundPicker from './components/FundPicker.jsx';
import SummaryCards from './components/SummaryCards.jsx';
import CompareTab from './components/CompareTab.jsx';
import OverlapTab from './components/OverlapTab.jsx';
import FundamentalsTab from './components/FundamentalsTab.jsx';
import { fundColor } from './fundData.js';
import { compareFunds, getFundamentals, getHoldingsOverlapWithProgress } from '../../services/fundAnalyzerService.js';
import { getUserEmail } from '../../services/authService.js';
import { exportFundReport } from './exportFundReport.js';
import './FundAnalyzerPage.css';

const TABS = [
  { key: 'compare', label: 'Compare', icon: List, Component: CompareTab },
  { key: 'overlap', label: 'Overlap', icon: Blend, Component: OverlapTab },
  { key: 'fundamentals', label: 'Fundamentals', icon: PieChart, Component: FundamentalsTab },
];

// The selection survives a page refresh. Each signed-in user has their own, and only Clear all forgets it.
const storageKey = () => `mns_fund_analyzer:${getUserEmail() || 'guest'}`;
const FUND_FIELDS = ['scheme_code', 'scheme_name', 'amc', 'category', 'plan', 'option', 'isin'];

function readSaved() {
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey()) || 'null');
    return saved && Array.isArray(saved.funds) ? { funds: saved.funds.filter((fund) => fund && fund.scheme_code), analyzed: Boolean(saved.analyzed) } : { funds: [], analyzed: false };
  } catch {
    return { funds: [], analyzed: false };
  }
}

function writeSaved(funds, analyzed) {
  try {
    if (!funds.length) localStorage.removeItem(storageKey());
    else localStorage.setItem(storageKey(), JSON.stringify({ funds: funds.map((fund) => Object.fromEntries(FUND_FIELDS.map((key) => [key, fund[key] ?? null]))), analyzed }));
  } catch {
    // Storage can be full or blocked. The page still works; only the refresh memory is lost.
  }
}

export default function FundAnalyzerPage() {
  const [saved] = useState(readSaved);
  const [selectedFunds, setSelectedFunds] = useState(saved.funds);
  const [analyzed, setAnalyzed] = useState(saved.analyzed);
  const [saving, setSaving] = useState(false);
  const [funds, setFunds] = useState([]);
  const [activeTab, setActiveTab] = useState('compare');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [source, setSource] = useState('AMFI');
  const [asOfDate, setAsOfDate] = useState(null);
  const [riskFreeRate, setRiskFreeRate] = useState(0.065);
  const [fundMetadata, setFundMetadata] = useState({});
  const [fundamentalsLoading, setFundamentalsLoading] = useState(false);
  const [fundamentalsError, setFundamentalsError] = useState('');
  const [overlap, setOverlap] = useState(null);
  const [overlapLoading, setOverlapLoading] = useState(false);
  const [overlapStage, setOverlapStage] = useState('');
  const overlapRequest = useRef(0);

  const loadOverlap = (schemeCodes, portfolioMonth) => {
    const requestId = ++overlapRequest.current;
    const current = () => overlapRequest.current === requestId;
    setOverlap(null);
    setOverlapStage('Fetching portfolio holdings...');
    setOverlapLoading(true);
    getHoldingsOverlapWithProgress(schemeCodes, portfolioMonth, (message) => { if (current()) setOverlapStage(message); }, () => !current())
      .then((payload) => { if (current() && payload) setOverlap(payload); })
      // Say why, never a figure: the message is the server's own reason.
      .catch((err) => { if (current()) setOverlap({ available: false, funds: [], highest_pair: null, highest_overlap: { available: false, percentage: null, message: err.message || 'Portfolio holdings unavailable' } }); })
      .finally(() => { if (current()) setOverlapLoading(false); });
  };

  const load = async (fundsToLoad = selectedFunds) => {
    setLoading(true); setError(''); setAnalyzed(true);
    const schemeCodes = fundsToLoad.map((fund) => fund.scheme_code);
    loadOverlap(schemeCodes);
    try {
      const payload = await compareFunds(schemeCodes);
      setFunds((payload.funds || []).map((fund, index) => ({ ...fund, color: fundColor(index) })));
      setSource(payload.source || 'AMFI'); setAsOfDate(payload.asOfDate || null); setRiskFreeRate(payload.summary?.riskFreeRate ?? 0.065);
    } catch (err) { setError(err.message || 'Unable to load live fund data.'); }
    finally { setLoading(false); }
  };

  // Load as soon as the funds are known, so the tab is ready when it is opened.
  useEffect(() => {
    if (!funds.length) { setFundamentalsLoading(false); return undefined; }
    const missing = funds.filter((fund) => !fundMetadata[fund.scheme_code]);
    if (!missing.length) { setFundamentalsLoading(false); return undefined; }
    let cancelled = false;
    setFundamentalsLoading(true);
    setFundamentalsError('');
    getFundamentals(missing.map((fund) => fund.scheme_code))
      .then((payload) => {
        if (cancelled) return;
        setFundMetadata((current) => ({
          ...current,
          ...Object.fromEntries((payload.funds || []).map((fund) => [fund.scheme_code, fund])),
        }));
      })
      .catch((err) => { if (!cancelled) setFundamentalsError(err.message || 'Unable to load fund metadata.'); })
      .finally(() => { if (!cancelled) setFundamentalsLoading(false); });
    return () => { cancelled = true; };
  }, [funds, fundMetadata]);

  useEffect(() => { writeSaved(selectedFunds, analyzed); }, [selectedFunds, analyzed]);

  // After a refresh, show the analysis again if it had been run for these funds.
  const autoLoaded = useRef(false);
  useEffect(() => {
    // Once only, even where React runs effects twice in development.
    if (autoLoaded.current) return;
    autoLoaded.current = true;
    if (saved.analyzed && saved.funds.length) load(saved.funds);
    // Runs once, on the first render only.
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const saveReport = async () => {
    setSaving(true);
    try {
      await exportFundReport({ funds, overlap, overlapLoading, fundMetadata, fundamentalsLoading, source, asOfDate, riskFreeRate });
    } catch (err) {
      setError(`The report could not be saved: ${err.message || 'unknown error'}`);
    } finally {
      setSaving(false);
    }
  };

  useEffect(() => { const handleKey = (event) => { if (event.key === '/' && !['INPUT', 'TEXTAREA'].includes(event.target.tagName)) { event.preventDefault(); document.querySelector('.fa-search__box input')?.focus(); } }; window.addEventListener('keydown', handleKey); return () => window.removeEventListener('keydown', handleKey); }, []);

  const { Component } = TABS.find((tab) => tab.key === activeTab);
  const displayedFunds = activeTab === 'fundamentals'
    ? funds.map((fund) => {
      const metadata = fundMetadata[fund.scheme_code] || {};
      return { ...fund, ...metadata, ter: metadata.ter ?? fund.ter, ter_as_of: metadata.ter_as_of ?? fund.ter_as_of, metrics: fund.metrics ?? metadata.metrics };
    })
    : funds;
  return <div className="fa">
    <header className="fa__header"><div><p className="fa__eyebrow"><i /> LIVE {source}</p><h1>Fund Analyzer</h1><p className="fa__sub">Compare live AMFI NAV history, returns, risk, holdings overlap and published fund metadata for the schemes you select.</p></div><div className="fa__actions"><button type="button" className="fa__action" onClick={saveReport} disabled={!funds.length || saving} title={funds.length ? 'Save this analysis as a PDF' : 'Run Update analysis first'}><Download size={15} /> {saving ? 'Saving…' : 'Save report'}</button></div></header>
    <FundPicker selectedFunds={selectedFunds} onChange={setSelectedFunds} onClear={() => { overlapRequest.current += 1; setSelectedFunds([]); setAnalyzed(false); setFunds([]); setFundMetadata({}); setOverlap(null); setOverlapLoading(false); }} onUpdate={() => load()} loading={loading} />
    {error && <p className="fa-notice" role="alert">{error}</p>}
    {!funds.length && !loading ? <p className="fa-card fa__empty">Select AMFI schemes and press Update analysis to load live figures.</p> : <>{funds.length > 0 && <><SummaryCards funds={funds} overlap={overlap} overlapLoading={overlapLoading} overlapStage={overlapStage} /><div className="fa__tabbar"><nav className="fa__tabs" role="tablist">{TABS.map(({ key, label, icon: Icon }) => <button key={key} type="button" role="tab" aria-selected={activeTab === key} className={`fa__tab${activeTab === key ? ' fa__tab--active' : ''}`} onClick={() => setActiveTab(key)}><Icon size={15} /> {label}</button>)}</nav><span className="fa__navdate">{source} as of {asOfDate || '—'}</span></div><Component funds={displayedFunds} overlap={overlap} overlapLoading={overlapLoading} overlapStage={overlapStage} onHistoricalMonth={(month) => loadOverlap(funds.map((fund) => fund.scheme_code), month)} riskFreeRate={riskFreeRate} loading={fundamentalsLoading} error={fundamentalsError} /></>}</>}
    <p className="fa__disclaimer">Mutual fund investments are subject to market risks; read all scheme-related documents carefully. Past performance may or may not be sustained in future.</p>
  </div>;
}
