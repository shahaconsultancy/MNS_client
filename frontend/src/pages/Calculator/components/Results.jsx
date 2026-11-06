// tone: 'hero' (navy), 'accent' (gold tint) or 'plain' (outlined).
export function ResultCard({ label, value, note, tone = 'plain', valueClass = '' }) {
  return (
    <div className={`calc-result calc-result--${tone}`}>
      <span className="calc-result__label">{label}</span>
      <strong className={`calc-result__value ${valueClass}`}>{value}</strong>
      {note && <span className="calc-result__note">{note}</span>}
    </div>
  );
}

export function ResultRow({ children }) {
  return <div className="calc-result-row">{children}</div>;
}

// rows: [{ label, value }]; total: { label, value } shown under a rule.
export function Breakdown({ rows, total }) {
  return (
    <dl className="calc-breakdown">
      {rows.map((r, i) => (
        <div key={i} className="calc-breakdown__row">
          <dt>{r.label}</dt>
          <dd>{r.value}</dd>
        </div>
      ))}
      {total && (
        <div className="calc-breakdown__row calc-breakdown__row--total">
          <dt>{total.label}</dt>
          <dd>{total.value}</dd>
        </div>
      )}
    </dl>
  );
}
