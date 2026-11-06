export const inr = (n) => `${n < 0 ? '-' : ''}₹${Math.abs(n).toLocaleString('en-IN')}`;
export const pct = (n) => `${n.toFixed(1)}%`;

// SIP growth factor: monthly compounding, instalments paid at the start of each month.
export const sipFactor = (rate, months) => {
  const m = rate / 12;
  return m ? (((1 + m) ** months - 1) / m) * (1 + m) : months;
};

// Chart axis label: 77.9L, or 1.0Cr from a crore up.
export const lakhs = (v) => (v >= 1e7 ? `${(v / 1e7).toFixed(1)}Cr` : v ? `${(v / 1e5).toFixed(1)}L` : "0");
