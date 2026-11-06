import { useState } from 'react';
import { Eye, EyeOff } from 'lucide-react';
import './InputField.css';

export default function InputField({
  id,
  label,
  type = 'text',
  icon: Icon,
  error,
  labelAside,
  ...inputProps
}) {
  const [visible, setVisible] = useState(false);
  const isPassword = type === 'password';

  return (
    <div className="field">
      <div className="field__top">
        <label htmlFor={id} className="field__label">{label}</label>
        {labelAside}
      </div>
      <div className={`field__box ${error ? 'field__box--error' : ''}`}>
        {Icon && <Icon size={16} className="field__icon" aria-hidden="true" />}
        <input
          id={id}
          className="field__input"
          type={isPassword && visible ? 'text' : type}
          aria-invalid={Boolean(error)}
          {...inputProps}
        />
        {isPassword && (
          <button
            type="button"
            className="field__toggle"
            onClick={() => setVisible((v) => !v)}
            aria-label={visible ? 'Hide password' : 'Show password'}
          >
            {visible ? <EyeOff size={16} /> : <Eye size={16} />}
          </button>
        )}
      </div>
      {error && <p className="field__error" role="alert">{error}</p>}
    </div>
  );
}
