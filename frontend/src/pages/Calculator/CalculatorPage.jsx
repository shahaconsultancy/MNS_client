import { useState } from 'react';
import { ArrowDownUp, Layers, Percent, Printer, RotateCcw, Target, TrendingUp, Wallet } from 'lucide-react';
import InflationCalculator, { defaults as inflationDefaults } from './calculators/InflationCalculator.jsx';
import GoalCalculator, { defaults as goalDefaults } from './calculators/GoalCalculator.jsx';
import SipCalculator, { defaults as sipDefaults } from './calculators/SipCalculator.jsx';
import SwpCalculator, { defaults as swpDefaults } from './calculators/SwpCalculator.jsx';
import SipLumpsumCalculator, { defaults as sipLumpsumDefaults } from './calculators/SipLumpsumCalculator.jsx';
import SipLumpsumSwpCalculator, { defaults as sipLumpsumSwpDefaults } from './calculators/SipLumpsumSwpCalculator.jsx';
import './CalculatorPage.css';

const TABS = [
  { key: 'inflation', label: 'Inflation', icon: Percent, Component: InflationCalculator, defaults: inflationDefaults },
  { key: 'goal', label: 'Goal Based', icon: Target, Component: GoalCalculator, defaults: goalDefaults },
  { key: 'sip', label: 'SIP', icon: TrendingUp, Component: SipCalculator, defaults: sipDefaults },
  { key: 'swp', label: 'SWP', icon: Wallet, Component: SwpCalculator, defaults: swpDefaults },
  { key: 'sipLumpsum', label: 'SIP + Lumpsum', icon: Layers, Component: SipLumpsumCalculator, defaults: sipLumpsumDefaults },
  { key: 'sipLumpsumSwp', label: 'SIP + Lumpsum + SWP', icon: ArrowDownUp, Component: SipLumpsumSwpCalculator, defaults: sipLumpsumSwpDefaults },
];

const initialValues = () => Object.fromEntries(TABS.map((t) => [t.key, { ...t.defaults }]));

export default function CalculatorPage() {
  const [active, setActive] = useState(TABS[0].key);
  // Inputs for every calculator, so switching tabs keeps what was entered.
  const [values, setValues] = useState(initialValues);

  const tab = TABS.find((t) => t.key === active);
  const { Component } = tab;

  const handleChange = (field, value) =>
    setValues((prev) => ({ ...prev, [active]: { ...prev[active], [field]: value } }));

  const handleReset = () => setValues((prev) => ({ ...prev, [active]: { ...tab.defaults } }));

  return (
    <div className="calc">
      <header className="calc__header">
        <div>
          <p className="calc__eyebrow">PLANNING TOOLS</p>
          <h1>Calculators</h1>
          <p className="calc__sub">Model inflation, goals, SIPs and withdrawals for any client.</p>
        </div>
        <div className="calc__actions">
          <button type="button" className="calc__action" onClick={handleReset}><RotateCcw size={15} /> Reset</button>
          <button type="button" className="calc__action" onClick={() => window.print()}><Printer size={15} /> Print</button>
        </div>
      </header>

      <nav className="calc__tabs" role="tablist">
        {TABS.map(({ key, label, icon: Icon }) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={active === key}
            className={`calc__tab${active === key ? ' calc__tab--active' : ''}`}
            onClick={() => setActive(key)}
          >
            <Icon size={15} /> {label}
          </button>
        ))}
      </nav>

      <Component values={values[active]} onChange={handleChange} />
    </div>
  );
}
