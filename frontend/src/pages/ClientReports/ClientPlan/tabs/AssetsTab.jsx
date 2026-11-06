import { useState } from 'react';
import { Building2, Car, Droplet, Gem, Landmark, TrendingUp } from 'lucide-react';
import { inr } from '../format.js';
import { AmountInput, CategoryRow, ShareCell, TabFooter } from '../PlanParts.jsx';

export const CLASSES = [
  {
    key: 'liquid', label: 'Liquid assets', icon: Droplet, tone: 'blue',
    items: ['Savings bank', 'Liquid funds', 'Cash in hand'],
  },
  {
    key: 'debt', label: 'Debt assets', icon: Landmark, tone: 'purple',
    items: [
      'Debt fixed deposits',
      'PPF (Public Provident Fund)',
      'EPF / SA / gratuity (current value)',
      'NSC / POMIS / KVP / RDs',
      'NPS (debt portion)',
      'Debt mutual funds',
      'Insurance policies – surrender value',
      'Bonds / debentures / CDs',
    ],
  },
  {
    key: 'equity', label: 'Equity assets', icon: TrendingUp, tone: 'green',
    items: ['Direct equity / PMS / AIF', 'Equity mutual funds', 'NPS (equity portion)', 'Unit-linked insurance SV (ULIPs)'],
  },
  { key: 'gold', label: 'Gold assets', icon: Gem, tone: 'yellow', items: ['Gold ETFs / mutual funds', 'Gold bars / coins'] },
  {
    key: 'realestate', label: 'Real estate assets', icon: Building2, tone: 'red',
    items: ['Apartment / house (as investment)', 'Plot / land / holiday home', 'Real estate funds / REITs'],
  },
  {
    key: 'personal', label: 'Personal assets', icon: Car, tone: 'gray',
    items: ['Residential home (self-occupied)', 'Vehicles', 'Jewellery, gadgets & other personal items'],
  },
];

// `data` holds the amount under `${class}-${i}` and the purpose under `${class}-${i}Goal`.
const itemKey = (cls, i) => `${cls.key}-${i}`;
const sum = (cls, data) => cls.items.reduce((s, _, i) => s + (Number(data[itemKey(cls, i)]) || 0), 0);

export const assetsTotal = (data) => CLASSES.reduce((s, c) => s + sum(c, data), 0);
export const personalAssetsTotal = (data) => sum(CLASSES.find((c) => c.key === 'personal'), data);
export const assetBreakdown = (data) => CLASSES.map((c) => ({ label: c.label.replace(' assets', ''), tone: c.tone, value: sum(c, data) }));

// Liquid, debt, equity and gold - what the family could draw on to cover a life-cover need.
const INVESTIBLE = ['liquid', 'debt', 'equity', 'gold'];
export const investibleAssetsTotal = (data) =>
  CLASSES.filter((c) => INVESTIBLE.includes(c.key)).reduce((s, c) => s + sum(c, data), 0);

export default function AssetsTab({ assetData, onAssetsChange, onBack, onNext, prevLabel, nextLabel }) {
  const [open, setOpen] = useState({ liquid: true });
  const total = assetsTotal(assetData);
  const share = (n) => (total ? (n / total) * 100 : 0);
  const set = (field, value) => onAssetsChange({ ...assetData, [field]: value });

  return (
    <section className="plan-card plan-card--flush">
      <div className="plan-card__pad">
        <h2>Assets</h2>
        <p className="plan-card__sub">Present value of everything the household owns, grouped by asset class.</p>
      </div>

      <div className="plan-table">
        <div className="plan-row plan-row--purpose plan-row--head">
          <span>ASSET CLASS / ITEM</span><span>PRESENT VALUE</span><span>PURPOSE / GOAL</span><span>% OF ASSETS</span>
        </div>

        {CLASSES.map((cls) => {
          const isOpen = Boolean(open[cls.key]);
          const clsTotal = sum(cls, assetData);
          const filled = cls.items.filter((_, i) => Number(assetData[itemKey(cls, i)]) > 0).length;
          return (
            <div key={cls.key}>
              <CategoryRow
                className="plan-row--purpose"
                icon={cls.icon}
                label={cls.label}
                tone={cls.tone}
                isOpen={isOpen}
                onToggle={() => setOpen({ ...open, [cls.key]: !isOpen })}
                filled={filled}
                count={cls.items.length}
                total={clsTotal}
                extra={<span />}
                percent={share(clsTotal)}
              />

              {isOpen && cls.items.map((label, i) => {
                const key = itemKey(cls, i);
                return (
                  <div key={key} className="plan-row plan-row--purpose exp__item">
                    <div className="plan-source">
                      <span className="exp__index">{i + 1}</span>
                      <strong>{label}</strong>
                    </div>
                    <AmountInput label={label} value={assetData[key]} onChange={(v) => set(key, v)} />
                    <input
                      type="text"
                      className="plan-textbox"
                      placeholder="Purpose / goal"
                      maxLength={100}
                      aria-label={`${label} purpose`}
                      value={assetData[`${key}Goal`] || ''}
                      onChange={(e) => set(`${key}Goal`, e.target.value)}
                    />
                    <ShareCell percent={share(Number(assetData[key]) || 0)} tone={cls.tone} />
                  </div>
                );
              })}
            </div>
          );
        })}

        <div className="plan-row plan-row--purpose plan-row--total">
          <strong>Total assets</strong>
          <strong>{inr(total)}</strong>
          <span />
          <strong>100%</strong>
        </div>
      </div>

      <TabFooter onBack={onBack} onNext={onNext} prevLabel={prevLabel} nextLabel={nextLabel} />
    </section>
  );
}
