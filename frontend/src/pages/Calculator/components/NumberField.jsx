// Numeric input that keeps the raw string in state; money fields show Indian digit grouping.
export default function NumberField({ label, value, onChange, kind = 'money', hint }) {
  const decimals = kind === 'percent';
  const display = kind === 'money' && value !== '' ? Number(value).toLocaleString('en-IN') : value;

  const handleChange = (e) => {
    let raw = e.target.value.replace(decimals ? /[^\d.]/g : /\D/g, '');
    if (decimals) raw = raw.replace(/(\..*)\./g, '$1');
    onChange(raw.slice(0, 13));
  };

  return (
    <label className="calc-field">
      <span className="calc-field__label">{label}</span>
      <span className="calc-field__box">
        {kind === 'money' && <span className="calc-field__affix">₹</span>}
        <input type="text" inputMode={decimals ? 'decimal' : 'numeric'} value={display} onChange={handleChange} />
        {kind === 'percent' && <span className="calc-field__affix">%</span>}
        {kind === 'years' && <span className="calc-field__affix">yrs</span>}
      </span>
      {hint && <span className="calc-field__hint">{hint}</span>}
    </label>
  );
}
