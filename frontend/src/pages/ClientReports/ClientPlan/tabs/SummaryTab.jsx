import { AlignLeft, DollarSign, Landmark, PieChart, Printer } from 'lucide-react';
import { inr, lakhs, pct } from '../format.js';
import { assetBreakdown } from './AssetsTab.jsx';
import { expenseBreakdown } from './ExpensesTab.jsx';
import { savingsTotal } from './SavingsTab.jsx';

// Same hues as the .tone--* classes, for the SVG rings.
const HEX = { blue: '#3b5bdb', yellow: '#c29035', purple: '#7048e8', green: '#2f9e6b', red: '#e8590c', gray: '#6b7385' };
const R = 62;
const CIRC = 2 * Math.PI * R;

const share = (n, of) => (of > 0 ? (n / of) * 100 : 0);
const crore = (n) => `≈ ₹${(n / 1e7).toFixed(2)} Cr`;

function Stat({ label, value, note, cls = '' }) {
  return (
    <div className={`plan__stat ${cls}`}>
      <span className="plan__stat-label">{label}</span>
      <strong>{value}</strong>
      {note && <small>{note}</small>}
    </div>
  );
}

function Donut({ rows, total }) {
  let offset = 0;
  return (
    <svg className="sum__donut" viewBox="0 0 160 160" role="img" aria-label="Asset allocation">
      <g transform="rotate(-90 80 80)" fill="none" strokeWidth="26">
        <circle cx="80" cy="80" r={R} stroke="#ece9e1" />
        {rows.filter((r) => r.value > 0).map((r) => {
          const len = (r.value / total) * CIRC;
          const seg = (
            <circle key={r.label} cx="80" cy="80" r={R} stroke={HEX[r.tone]} strokeDasharray={`${len} ${CIRC - len}`} strokeDashoffset={-offset} />
          );
          offset += len;
          return seg;
        })}
      </g>
      <text x="80" y="74" textAnchor="middle" className="sum__donut-label">TOTAL</text>
      <text x="80" y="94" textAnchor="middle" className="sum__donut-value">₹{lakhs(total)}</text>
    </svg>
  );
}

export default function SummaryTab({
  displayName, info, incomeTotal, expensesAmount, expenses, savings, assetData, assetsTotal, personalAssets,
  liabilitiesAmount, onBack, prevLabel,
}) {
  const monthlySavings = incomeTotal - expensesAmount;
  const invested = savingsTotal(savings);
  const netWorth = assetsTotal - liabilitiesAmount;
  const netWorthExcl = netWorth - personalAssets;
  const planDate = info?.planDate && new Date(info.planDate).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });

  const assets = assetBreakdown(assetData).sort((a, b) => b.value - a.value);
  const spend = expenseBreakdown(expenses);
  const biggest = Math.max(...spend.map((s) => s.value), 1);

  return (
    <section className="plan-card plan-card--flush">
      <div className="plan-card__pad">
        <h2>Plan summary</h2>
        <p className="plan-card__sub">Snapshot of {displayName}'s finances{planDate && ` · plan date ${planDate}`}</p>
      </div>

      <div className="ins__section">
        <h3 className="plan-section"><DollarSign size={15} /> MONTHLY CASH FLOW</h3>
        <div className="plan-stats">
          <Stat label="INCOME" value={inr(incomeTotal)} note={`${inr(incomeTotal * 12)} / year`} />
          <Stat label="EXPENSES" value={inr(expensesAmount)} note={`${inr(expensesAmount * 12)} / year`} />
          <Stat label="SAVINGS" value={inr(monthlySavings)} note={`Savings rate ${pct(share(monthlySavings, incomeTotal))}`} cls="plan__stat--dark" />
          <Stat
            label="SURPLUS FOR NEW INVESTMENTS" value={inr(monthlySavings - invested)}
            note={`After ongoing investments of ${inr(invested)}/mo`} cls="sum__green"
          />
        </div>
      </div>

      <div className="sum__split">
        <div className="sum__col">
          <h3 className="plan-section"><PieChart size={15} /> ASSET ALLOCATION</h3>
          <div className="sum__alloc">
            <Donut rows={assets} total={assetsTotal} />
            <ul className="sum__legend">
              {assets.map((a) => (
                <li key={a.label}>
                  <i className={`tone--${a.tone}`} /><span>{a.label}</span>
                  <b>{inr(a.value)}</b><em>{pct(share(a.value, assetsTotal))}</em>
                </li>
              ))}
            </ul>
          </div>
        </div>

        <div className="sum__col">
          <h3 className="plan-section"><AlignLeft size={15} /> EXPENSE BREAKDOWN (MONTHLY)</h3>
          {spend.map((s) => (
            <div key={s.label} className="sum__bar-row">
              <div><span>{s.label}</span><b>{inr(s.value)}</b><em>{pct(share(s.value, incomeTotal))}</em></div>
              <i><b className={`tone--${s.tone}`} style={{ width: `${(s.value / biggest) * 100}%` }} /></i>
            </div>
          ))}
          <p className="sum__foot">Percentages are of monthly income.</p>
        </div>
      </div>

      <div className="ins__section">
        <h3 className="plan-section"><Landmark size={15} /> NET WORTH STATEMENT</h3>
        <div className="plan-stats">
          <Stat label="TOTAL ASSETS" value={inr(assetsTotal)} />
          <Stat label="TOTAL LIABILITIES" value={inr(liabilitiesAmount)} note={`Debt-to-assets ${pct(share(liabilitiesAmount, assetsTotal))}`} />
          <Stat label="NET WORTH (INCL. PERSONAL)" value={inr(netWorth)} note={crore(netWorth)} cls="plan__stat--dark" />
          <Stat
            label="NET WORTH (EXCL. PERSONAL)" value={inr(netWorthExcl)}
            note={`${crore(netWorthExcl)} · excludes ${inr(personalAssets)}`} cls="plan__stat--dark"
          />
        </div>
      </div>

      <footer className="plan-footer plan-footer--split">
        <button type="button" className="plan-next" onClick={onBack}>← {prevLabel}</button>
        <button type="button" className="plan-next" onClick={() => window.print()}><Printer size={14} /> Print summary</button>
      </footer>
    </section>
  );
}
