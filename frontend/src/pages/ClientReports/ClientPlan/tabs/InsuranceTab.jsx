import { Heart, Pencil, ShieldCheck, ShieldAlert } from 'lucide-react';
import { inr } from '../format.js';
import { TabFooter } from '../PlanParts.jsx';

const HEALTH_BASE = 500000; // floater for the first member
const HEALTH_EXTRA = 300000; // each additional member
const HEALTH_FLOOR_RATE = 0.5; // of annual income

const digits = (v) => v.replace(/\D/g, '').slice(0, 12);

function coverStatus(recommended, existing) {
  if (existing >= recommended) return { label: 'Adequate', tone: 'ok' };
  if (existing === 0) return { label: 'No cover', tone: 'bad' };
  return { label: 'Under-insured', tone: 'warn' };
}

// One result card (life or health): status, coverage bar, three figures and the working.
function CoverCard({ icon: Icon, title, iconClass, recommended, existing, rows }) {
  const status = coverStatus(recommended, existing);
  const covered = recommended ? Math.min((existing / recommended) * 100, 100) : 100;
  const diff = existing - recommended;
  const StatusIcon = status.tone === 'ok' ? ShieldCheck : ShieldAlert;

  return (
    <article className="ins__card">
      <header className="ins__card-head">
        <span className={`plan-icon ${iconClass}`}><Icon size={18} /></span>
        <h3>{title}</h3>
        <span className={`ins__badge ins__badge--${status.tone}`}><StatusIcon size={12} /> {status.label}</span>
      </header>

      <div className="ins__cover">
        <span>Cover in place</span><strong>{Math.round(covered)}% covered</strong>
      </div>
      <div className="ins__bar"><b className={`ins__fill--${status.tone}`} style={{ width: `${covered}%` }} /></div>

      <div className="ins__figures">
        <div><span>RECOMMENDED</span><strong>{inr(recommended)}</strong></div>
        <div><span>EXISTING</span><strong>{inr(existing)}</strong></div>
        <div className={diff >= 0 ? 'ins__fig--ok' : 'ins__fig--bad'}>
          <span>{diff >= 0 ? 'SURPLUS' : 'GAP'}</span><strong>{inr(Math.abs(diff))}</strong>
        </div>
      </div>

      <dl className="ins__rows">
        {rows.map(([label, value]) => (
          <div key={label}><dt>{label}</dt><dd>{value}</dd></div>
        ))}
        <div className="ins__rows-total"><dt>Recommended cover</dt><dd>{inr(recommended)}</dd></div>
      </dl>
    </article>
  );
}

// Recommended and existing life / health cover; shared with the report export.
export function insuranceCalc({ insurance, incomeTotal, liabilitiesAmount, investibleAmount, members, familyLife, familyHealth }) {
  const years = Number(insurance.years) || 0;
  const annualIncome = incomeTotal * 12;
  // With auto-sum on, cover is the total entered on the Family Members tab.
  const lifeExisting = insurance.auto ? familyLife : Number(insurance.lifeCover) || 0;
  const healthExisting = insurance.auto ? familyHealth : Number(insurance.healthCover) || 0;

  const lifeRecommended = Math.max(0, annualIncome * years + liabilitiesAmount - investibleAmount);
  const floater = HEALTH_BASE + HEALTH_EXTRA * Math.max(members - 1, 0);
  const floor = annualIncome * HEALTH_FLOOR_RATE;
  const healthRecommended = Math.max(floater, floor);
  return {
    years, annualIncome, lifeExisting, healthExisting, lifeRecommended, floater, floor, healthRecommended,
    lifeStatus: coverStatus(lifeRecommended, lifeExisting).label,
    healthStatus: coverStatus(healthRecommended, healthExisting).label,
  };
}

export default function InsuranceTab({
  insurance, onInsuranceChange, incomeTotal, liabilitiesAmount, investibleAmount, members, familyLife, familyHealth,
  onBack, onNext, prevLabel, nextLabel,
}) {
  const set = (field, value) => onInsuranceChange({ ...insurance, [field]: value });
  const {
    years, annualIncome, lifeExisting, healthExisting, lifeRecommended, floater, floor, healthRecommended,
  } = insuranceCalc({ insurance, incomeTotal, liabilitiesAmount, investibleAmount, members, familyLife, familyHealth });

  return (
    <section className="plan-card plan-card--flush">
      <div className="plan-card__pad">
        <h2>Insurance gap check</h2>
        <p className="plan-card__sub">Is the family's life and health cover enough for its income, loans and size?</p>
      </div>

      <div className="ins__section">
        <h3 className="plan-section"><Pencil size={15} /> INPUTS</h3>
        <div className="plan-grid plan-grid--3">
          <label className="plan-field">
            <span>Income replacement years</span>
            <div className="plan-input">
              <input type="text" inputMode="numeric" value={insurance.years} onChange={(e) => set('years', digits(e.target.value).slice(0, 2))} />
              <em>years</em>
            </div>
            <small>Years of income the family would need replaced</small>
          </label>
          <label className="plan-field">
            <span>Existing life cover</span>
            <div className={`plan-input${insurance.auto ? ' plan-input--disabled' : ''}`}>
              <em>₹</em>
              <input
                type="text" inputMode="numeric" placeholder="0" disabled={insurance.auto}
                value={insurance.auto ? '' : insurance.lifeCover} onChange={(e) => set('lifeCover', digits(e.target.value))}
              />
            </div>
            <small>Total sum assured across term / life policies</small>
          </label>
          <label className="plan-field">
            <span>Existing health cover</span>
            <div className={`plan-input${insurance.auto ? ' plan-input--disabled' : ''}`}>
              <em>₹</em>
              <input
                type="text" inputMode="numeric" placeholder="0" disabled={insurance.auto}
                value={insurance.auto ? '' : insurance.healthCover} onChange={(e) => set('healthCover', digits(e.target.value))}
              />
            </div>
            <small>Base + top-up / super top-up cover</small>
          </label>
        </div>

        <div className="ins__auto">
          <button
            type="button" role="switch" aria-checked={insurance.auto}
            className={`ins__switch${insurance.auto ? ' ins__switch--on' : ''}`}
            onClick={() => set('auto', !insurance.auto)}
          ><i /></button>
          <span>Auto-sum existing cover from the Family Members tab</span>
          {insurance.auto && (
            <small>{members > 1 ? `Summed across ${members - 1} family member${members === 2 ? '' : 's'}` : 'No family members added yet'}</small>
          )}
        </div>
      </div>

      <div className="ins__section ins__section--grey">
        <div className="ins__cards">
          <CoverCard
            icon={Heart} title="Life insurance" iconClass="tone-bg--blue"
            recommended={lifeRecommended} existing={lifeExisting}
            rows={[
              [`Annual income × ${years} years`, inr(annualIncome * years)],
              ['+ Outstanding liabilities', inr(liabilitiesAmount)],
              [<>− Investible assets <small>(liquid, debt, equity, gold)</small></>, inr(investibleAmount)],
            ]}
          />
          <CoverCard
            icon={ShieldCheck} title="Health insurance" iconClass="tone-bg--green"
            recommended={healthRecommended} existing={healthExisting}
            rows={[
              ['Family floater for', `${members} member${members === 1 ? '' : 's'}`],
              ['Sized for members (₹5 L + ₹3 L each extra)', inr(floater)],
              ['Floor: 50% of annual income', inr(floor)],
            ]}
          />
        </div>
      </div>

      <TabFooter onBack={onBack} onNext={onNext} prevLabel={prevLabel} nextLabel={nextLabel} />
    </section>
  );
}
