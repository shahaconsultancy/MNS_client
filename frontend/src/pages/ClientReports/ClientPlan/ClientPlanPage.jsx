import { useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { ArrowLeft, ChevronDown, Download, FileSpreadsheet, FileText, Save, Upload } from 'lucide-react';
import { exportExcel, exportPdf } from './exportPlan.js';
import { readPlanFile } from './importPlan.js';
import { getPlan, listClients, savePlan } from '../../../services/clientService.js';
import { fromPlan, toPayload } from './planApi.js';
import { TABS } from './tabs.js';
import { inr, pct } from './format.js';
import { incomeTotal } from './tabs/IncomeTab.jsx';
import { expensesTotal } from './tabs/ExpensesTab.jsx';
import { familyCover } from './tabs/FamilyTab.jsx';
import { savingsTotal } from './tabs/SavingsTab.jsx';
import { liabilitiesTotal } from './tabs/LiabilitiesTab.jsx';
import { assetsTotal, investibleAssetsTotal, personalAssetsTotal } from './tabs/AssetsTab.jsx';
import './ClientPlan.css';

const today = () => new Date().toISOString().slice(0, 10);
const titleCase = (v) => v.toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase());
// "PRITESH VILAS SHAHA" -> first and last word, so the header reads "Mr. Pritesh Shaha" and the avatar "PS".
const firstLast = (full) => {
  const words = full.trim().split(/\s+/);
  return words.length > 1 ? `${words[0]} ${words[words.length - 1]}` : words[0];
};
const initials = (full) => firstLast(full).split(' ').map((w) => w[0]).join('').toUpperCase();

const DEFAULT_INSURANCE = { years: '40', lifeCover: '', healthCover: '', auto: false };
const DEFAULT_RETIREMENT = {
  expense: '', corpus: '', age: '35', retireAge: '60', life: '85', inflation: '6', before: '12', after: '8',
};

const ratio =(n, of) => (of > 0 ? Math.max(0, (n / of) * 100) : 0);

export default function ClientPlanPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const [client, setClient] = useState(null);
  const [error, setError] = useState('');
  const [active, setActive] = useState(0);
  const [info, setInfo] = useState(null);
  const [income, setIncome] = useState({ salary: '', bonus: '', business: '', rental: '', investment: '' });
  const [expenses, setExpenses] = useState({});
  const [savings, setSavings] = useState({});
  const [liabilityData, setLiabilityData] = useState({});
  const [assetData, setAssetData] = useState({});
  const [goals, setGoals] = useState([]);
  const [family, setFamily] = useState([]);
  const [retirement, setRetirement] = useState(DEFAULT_RETIREMENT);
  const [insurance, setInsurance] = useState(DEFAULT_INSURANCE);
  const [saved, setSaved] = useState('');
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState('');
  const [menuOpen, setMenuOpen] = useState(false);
  const [exporting, setExporting] = useState(false);
  const menuRef = useRef(null);
  const fileRef = useRef(null);
  const [pending, setPending] = useState(null);
  const [notice, setNotice] = useState('');

  useEffect(() => {
    const close = (e) => { if (menuRef.current && !menuRef.current.contains(e.target)) setMenuOpen(false); };
    document.addEventListener('mousedown', close);
    return () => document.removeEventListener('mousedown', close);
  }, []);

  useEffect(() => {
    listClients()
      .then(async (rows) => {
        const c = rows.find((r) => String(r.id) === id);
        if (!c) throw new Error('Client plan not found.');
        const defaults = {
          info: {
            clientName: c.full_name.toUpperCase(),
            spouseName: '',
            phone: c.mobile || '',
            email: c.email || '',
            address: '',
            planDate: today(),
            advisorNotes: '',
          },
          insurance: DEFAULT_INSURANCE,
          retirement: DEFAULT_RETIREMENT,
        };
        const plan = fromPlan(await getPlan(id), defaults);
        setInfo(plan.info);
        setIncome(plan.income);
        setExpenses(plan.expenses);
        setSavings(plan.savings);
        setAssetData(plan.assetData);
        setLiabilityData(plan.liabilityData);
        setInsurance(plan.insurance);
        setFamily(plan.family);
        setGoals(plan.goals);
        setRetirement(plan.retirement);
        setSaved(JSON.stringify(plan));
        setClient(c);
      })
      .catch((err) => setError(err.message));
  }, [id]);

  if (error) return <p className="plan-state plan-state--error" role="alert">{error}</p>;
  if (!client) return <p className="plan-state">Loading client plan…</p>;

  const tab = TABS[active];
  const prev = TABS[active - 1];
  const next = TABS[active + 1];
  const name = `${client.title}. ${titleCase(firstLast(client.full_name))}`;
  const incomeAmount = incomeTotal(income);
  const expensesAmount = expensesTotal(expenses);
  const savingsAmount = incomeAmount - expensesAmount; // the "Savings / mo" card
  const surplusAmount = savingsAmount - savingsTotal(savings); // what is left after the monthly investments
  const assets = assetsTotal(assetData);
  const personalAssets = personalAssetsTotal(assetData);
  const liabilities = liabilitiesTotal(liabilityData);
  const netWorth = assets - liabilities;

  // Everything the Save button writes to the database.
  const planState = { info, income, expenses, savings, assetData, liabilityData, insurance, family, goals, retirement };
  const snapshot = JSON.stringify(planState);
  const dirty = snapshot !== saved;

  const save = async () => {
    setSaving(true);
    setSaveError('');
    try {
      await savePlan(id, toPayload(planState));
      setSaved(snapshot);
    } catch (err) {
      setSaveError(err.message);
    } finally {
      setSaving(false);
    }
  };

  // Exports what is on screen, saved or not.
  const runExport = async (write) => {
    setMenuOpen(false);
    setExporting(true);
    setSaveError('');
    try {
      await write(planState);
    } catch (err) {
      setSaveError(`Export failed: ${err.message}`);
    } finally {
      setExporting(false);
    }
  };

  // Import: read the chosen .xlsx, ask before replacing, then fill the tabs found in the file. Nothing is saved until Save.
  const chooseFile = async (e) => {
    const file = e.target.files[0];
    e.target.value = '';
    if (!file) return;
    setSaveError('');
    setNotice('');
    if (!/\.xlsx$/i.test(file.name)) {
      setSaveError('Only Excel (.xlsx) files can be imported.');
      return;
    }
    setExporting(true);
    try {
      setPending({ ...(await readPlanFile(file, { insurance: DEFAULT_INSURANCE, retirement: DEFAULT_RETIREMENT })), fileName: file.name });
    } catch (err) {
      setSaveError(err.message);
    } finally {
      setExporting(false);
    }
  };

  const applyImport = () => {
    const { data, tabs, fields, unmatched } = pending;
    if (data.info) setInfo({ ...info, ...data.info });
    if (data.income) setIncome(data.income);
    if (data.expenses) setExpenses(data.expenses);
    if (data.savings) setSavings(data.savings);
    if (data.assetData) setAssetData(data.assetData);
    if (data.liabilityData) setLiabilityData(data.liabilityData);
    if (data.insurance) setInsurance(data.insurance);
    if (data.goals) setGoals(data.goals);
    if (data.retirement) setRetirement(data.retirement);
    if (data.family) setFamily(data.family);
    setNotice(
      `Imported ${tabs.length} tab${tabs.length === 1 ? '' : 's'}, ${fields} field${fields === 1 ? '' : 's'}`
      + `${unmatched ? ` (${unmatched} row${unmatched === 1 ? '' : 's'} not recognised)` : ''}. Click Save to keep the changes.`,
    );
    setPending(null);
  };

  const cards = [
    {
      label: 'INCOME / MO', color: '#2b3f86', value: incomeAmount,
      note: `₹${((incomeAmount * 12) / 100000).toFixed(1)} L per year`,
    },
    {
      label: 'EXPENSES / MO', color: '#b7791f', value: expensesAmount,
      bar: ratio(expensesAmount, incomeAmount), barClass: 'tone--yellow',
      note: <><b className="plan__pct plan__pct--amber">{pct(ratio(expensesAmount, incomeAmount))}</b> of income</>,
    },
    {
      label: 'SAVINGS / MO', color: '#2f9e6b', value: savingsAmount,
      bar: ratio(savingsAmount, incomeAmount), barClass: 'tone--green',
      note: <><b className="plan__pct plan__pct--green">{pct(ratio(savingsAmount, incomeAmount))}</b> savings rate</>,
    },
    { label: 'TOTAL ASSETS', color: '#c29035', value: assets, note: `Liabilities ${inr(liabilities)}` },
    {
      label: 'NET WORTH', color: '#997023', value: netWorth, dark: true,
      note: `≈ ₹${(netWorth / 10000000).toFixed(2)} Cr · ${pct(ratio(netWorth, assets))} of assets`,
    },
  ];

  return (
    <div className="plan">
      <header className="plan__header">
        <button type="button" className="plan__back" onClick={() => navigate('/client-reports')} aria-label="Back to client reports">
          <ArrowLeft size={16} />
        </button>
        <span className="plan__avatar">{initials(client.full_name)}</span>
        <div>
          <p className="plan__crumb">Client Reports / {client.plan_name}</p>
          <h1>
            {name}
            <span className={`plan__status${dirty ? '' : ' plan__status--ok'}`}>{dirty ? 'Unsaved changes' : 'All changes saved'}</span>
          </h1>
        </div>
        <div className="plan__actions">
          {saveError && <p className="plan__error" role="alert">{saveError}</p>}
          {notice && !saveError && <p className="plan__notice" role="status">{notice}</p>}
          <input
            ref={fileRef} type="file" hidden aria-label="Import Excel file"
            accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" onChange={chooseFile}
          />
          <button type="button" className="plan__export-btn" disabled={exporting} onClick={() => fileRef.current.click()}>
            <Upload size={15} /> Import
          </button>
          <div className="plan__export" ref={menuRef}>
            <button
              type="button" className="plan__export-btn" aria-haspopup="menu" aria-expanded={menuOpen}
              disabled={exporting} onClick={() => setMenuOpen(!menuOpen)}
            >
              <Download size={15} /> {exporting ? 'Exporting…' : 'Export'} <ChevronDown size={14} />
            </button>
            {menuOpen && (
              <div className="plan__menu" role="menu">
                <button type="button" role="menuitem" onClick={() => runExport(exportExcel)}>
                  <FileSpreadsheet size={16} className="plan__menu-xls" /> Excel workbook <small>.xlsx</small>
                </button>
                <button type="button" role="menuitem" onClick={() => runExport(exportPdf)}>
                  <FileText size={16} className="plan__menu-pdf" /> PDF report <small>.pdf</small>
                </button>
              </div>
            )}
          </div>
          <button type="button" className="plan__save" onClick={save} disabled={saving || !dirty}>
            <Save size={15} /> {saving ? 'Saving…' : 'Save'}
          </button>
        </div>
      </header>

      {pending && (
        <div className="plan-modal" role="dialog" aria-modal="true" aria-labelledby="import-title">
          <div className="plan-modal__box">
            <h2 id="import-title">Import from Excel?</h2>
            <p className="plan-modal__file">{pending.fileName}</p>
            <p>
              This fills <b>{pending.tabs.length} tab{pending.tabs.length === 1 ? '' : 's'}</b> ({pending.tabs.join(', ')}) and
              replaces what is there now. Nothing is saved until you click Save.
            </p>
            {pending.sourceName && pending.sourceName.trim().toLowerCase() !== (info.clientName || '').trim().toLowerCase() && (
              <p className="plan-modal__warn">
                This file was exported for <b>{pending.sourceName}</b>, but you are editing <b>{info.clientName || name}</b>.
              </p>
            )}
            <div className="plan-modal__actions">
              <button type="button" className="plan__export-btn" onClick={() => setPending(null)}>Cancel</button>
              <button type="button" className="plan__save" onClick={applyImport}>Import</button>
            </div>
          </div>
        </div>
      )}

      <section className="plan__summary">
        {cards.map((c) => (
          <div key={c.label} className={`plan__stat${c.dark ? ' plan__stat--dark' : ''}`}>
            <span className="plan__stat-label"><i style={{ background: c.color }} />{c.label}</span>
            <strong>{inr(c.value)}</strong>
            {c.bar !== undefined && (
              <div className="plan__stat-bar"><b className={c.barClass} style={{ width: `${Math.min(c.bar, 100)}%` }} /></div>
            )}
            <small>{c.note}</small>
          </div>
        ))}
      </section>

      <nav className="plan__tabs" aria-label="Plan sections">
        {TABS.map((t, i) => (
          <button
            key={t.key}
            type="button"
            className={`plan__tab${i === active ? ' plan__tab--active' : ''}`}
            onClick={() => setActive(i)}
          >
            <span>{i + 1}</span>{t.label}
          </button>
        ))}
      </nav>

      {tab.Component ? (
        <tab.Component
          info={info}
          onChange={setInfo}
          income={income}
          onIncomeChange={setIncome}
          expenses={expenses}
          onExpensesChange={setExpenses}
          displayName={name}
          retirement={retirement}
          onRetirementChange={setRetirement}
          goals={goals}
          onGoalsChange={setGoals}
          surplusAmount={surplusAmount}
          insurance={insurance}
          onInsuranceChange={setInsurance}
          liabilitiesAmount={liabilities}
          investibleAmount={investibleAssetsTotal(assetData)}
          members={1 + family.length}
          family={family}
          onFamilyChange={setFamily}
          familyLife={familyCover(family, 'lifeCover')}
          familyHealth={familyCover(family, 'healthCover')}
          assetData={assetData}
          onAssetsChange={setAssetData}
          liabilities={liabilityData}
          onLiabilitiesChange={setLiabilityData}
          assetsTotal={assets}
          personalAssets={personalAssets}
          savings={savings}
          onSavingsChange={setSavings}
          incomeTotal={incomeAmount}
          expensesAmount={expensesAmount}
          onBack={() => prev && setActive(active - 1)}
          onNext={() => next && setActive(active + 1)}
          prevLabel={prev?.label}
          nextLabel={next?.label}
        />
      ) : (
        <section className="plan-card"><h2>{tab.label}</h2><p className="plan-card__sub">Coming soon.</p></section>
      )}
    </div>
  );
}
