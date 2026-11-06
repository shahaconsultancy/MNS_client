import { Trash2, UserPlus, Users } from 'lucide-react';
import { NumberBox, TabFooter } from '../PlanParts.jsx';

export const RELATIONS = ['Spouse', 'Son', 'Daughter', 'Father', 'Mother', 'Other'];
export const GENDERS = ['Male', 'Female', 'Other'];

const newMember = (relation) => ({
  id: Date.now() + Math.random(), name: '', relation, dob: '', age: '', gender: '', occupation: '', income: '',
  lifePolicy: '', lifeCover: '', healthPolicy: '', healthCover: '', dependent: ['Son', 'Daughter'].includes(relation), notes: '',
});

export const ageOf = (dob) => {
  const d = new Date(dob);
  if (Number.isNaN(d.getTime())) return '';
  const now = new Date();
  const age = now.getFullYear() - d.getFullYear() - (now < new Date(now.getFullYear(), d.getMonth(), d.getDate()) ? 1 : 0);
  return age >= 0 ? String(age) : '';
};

// Existing cover summed over the family; the Insurance tab uses these when "auto-sum" is on.
export const familyCover = (family, field) => family.reduce((s, m) => s + (Number(m[field]) || 0), 0);

function Field({ label, opt, children }) {
  return <label className="plan-field"><span>{label}{opt && <small> ({opt})</small>}</span>{children}</label>;
}

function MemberCard({ m, onChange, onDelete }) {
  const set = (field) => (value) => onChange({ ...m, [field]: value });
  const text = (e) => set(e.target.name)(e.target.value);

  return (
    <article className="plan-item">
      <header className="plan-item__head">
        <span className="fam__avatar">{m.name ? m.name[0].toUpperCase() : '?'}</span>
        <input className="plan-item__name" name="name" type="text" maxLength={60} placeholder="Full name" aria-label="Full name" value={m.name} onChange={text} />
        <span className="fam__tag">{m.relation}</span>
        {m.dependent && <span className="fam__tag fam__tag--dep">Dependent</span>}
        <button type="button" className="plan-item__delete" onClick={onDelete} aria-label="Delete member"><Trash2 size={16} /></button>
      </header>

      <div className="plan-grid4 fam__body">
        <Field label="Relationship">
          <select name="relation" value={m.relation} onChange={text}>{RELATIONS.map((r) => <option key={r}>{r}</option>)}</select>
        </Field>
        <Field label="Date of birth"><input name="dob" type="date" value={m.dob} onChange={text} /></Field>
        <Field label="Age" opt="if no DOB">
          <input
            type="text" inputMode="numeric" placeholder="—" disabled={Boolean(m.dob)}
            value={m.dob ? ageOf(m.dob) : m.age} onChange={(e) => set('age')(e.target.value.replace(/\D/g, '').slice(0, 3))}
          />
        </Field>
        <Field label="Gender">
          <select name="gender" value={m.gender} onChange={text}>
            <option value="">Select</option>
            {GENDERS.map((g) => <option key={g}>{g}</option>)}
          </select>
        </Field>

        <Field label="Occupation"><input name="occupation" type="text" placeholder="e.g. Student, Homemaker" value={m.occupation} onChange={text} /></Field>
        <NumberBox label="Annual income" prefix="₹" value={m.income} onChange={set('income')} />
        <Field label="Life policy" opt="details"><input name="lifePolicy" type="text" placeholder="e.g. Term – LIC" value={m.lifePolicy} onChange={text} /></Field>
        <NumberBox label="Life cover" prefix="₹" value={m.lifeCover} onChange={set('lifeCover')} />

        <Field label="Health policy" opt="details"><input name="healthPolicy" type="text" placeholder="e.g. Family floater" value={m.healthPolicy} onChange={text} /></Field>
        <NumberBox label="Health cover" prefix="₹" value={m.healthCover} onChange={set('healthCover')} />
        <div className="plan-field">
          <span>Financially dependent</span>
          <div className="fam__toggle">
            <button
              type="button" role="switch" aria-checked={m.dependent} aria-label="Financially dependent"
              className={`ins__switch${m.dependent ? ' ins__switch--on' : ''}`} onClick={() => set('dependent')(!m.dependent)}
            ><i /></button>
            <b>{m.dependent ? 'Yes' : 'No'}</b>
          </div>
        </div>
        <Field label="Notes" opt="optional"><input name="notes" type="text" placeholder="Health, education, etc." value={m.notes} onChange={text} /></Field>
      </div>
    </article>
  );
}

export default function FamilyTab({ family, onFamilyChange, onBack, onNext, prevLabel, nextLabel }) {
  const count = family.length;
  const dependents = family.filter((m) => m.dependent).length;
  const hasSpouse = family.some((m) => m.relation === 'Spouse');
  const add = (relation) => onFamilyChange([...family, newMember(relation)]);
  const pills = RELATIONS.filter((r) => r !== 'Other' && !(r === 'Spouse' && hasSpouse));
  const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

  return (
    <section className="plan-card plan-card--flush">
      <div className="plan-card__pad plan-top">
        <div>
          <h2>Family members</h2>
          <p className="plan-card__sub">
            {count
              ? `${plural(count, 'member')} · ${plural(dependents, 'dependent')} · family of ${count + 1} including the client`
              : 'Details for each family member, used for insurance and goal planning.'}
          </p>
        </div>
        <button type="button" className="plan-add" onClick={() => add(hasSpouse ? 'Other' : 'Spouse')}>
          <UserPlus size={15} /> Add member
        </button>
      </div>

      <div className="plan-item__list">
        {count === 0 ? (
          <div className="fam__empty">
            <span className="fam__empty-icon"><Users size={24} /></span>
            <h3>No family members added yet</h3>
            <p>Add the spouse, children and parents. Their details feed the Insurance tab.</p>
            <div className="fam__pills">
              {pills.map((r) => <button key={r} type="button" className="fam__pill" onClick={() => add(r)}>+ {r}</button>)}
            </div>
          </div>
        ) : (
          <>
            {family.map((m, i) => (
              <MemberCard
                key={m.id}
                m={m}
                onChange={(next) => onFamilyChange(family.map((x, j) => (j === i ? next : x)))}
                onDelete={() => onFamilyChange(family.filter((_, j) => j !== i))}
              />
            ))}
            <div className="fam__pills fam__pills--row">
              <span>Quick add:</span>
              {pills.map((r) => <button key={r} type="button" className="fam__pill" onClick={() => add(r)}>+ {r}</button>)}
            </div>
          </>
        )}
      </div>

      <TabFooter onBack={onBack} onNext={onNext} prevLabel={prevLabel} nextLabel={nextLabel} />
    </section>
  );
}
