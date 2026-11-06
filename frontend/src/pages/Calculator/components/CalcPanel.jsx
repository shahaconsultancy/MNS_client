import { PencilLine } from 'lucide-react';
import NumberField from './NumberField.jsx';

// Left-hand card: title, one or more input sections, then the results.
export default function CalcPanel({ title, description, sections, values, onChange, children }) {
  return (
    <section className="calc-card calc-panel">
      <h2 className="calc-card__title">{title}</h2>
      <p className="calc-card__desc">{description}</p>

      {sections.map((s) => (
        <div key={s.heading} className="calc-panel__section">
          <p className="calc-panel__heading"><PencilLine size={14} /> {s.heading}</p>
          <div className="calc-panel__fields">
            {s.fields.map((f) => (
              <NumberField
                key={f.name}
                label={f.label}
                kind={f.kind}
                hint={f.hint}
                value={values[f.name]}
                onChange={(v) => onChange(f.name, v)}
              />
            ))}
          </div>
        </div>
      ))}

      <div className="calc-panel__results">{children}</div>
    </section>
  );
}
