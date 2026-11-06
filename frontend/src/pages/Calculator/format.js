export const inr = (n) => `${n < 0 ? '-' : ''}₹${Math.round(Math.abs(n)).toLocaleString('en-IN')}`;

// Short axis labels: 1.26Cr, 94.6L, 60K.
export const compact = (n) => {
  const trim = (v) => String(Number(v.toFixed(2)));
  const a = Math.abs(n);
  if (a >= 1e7) return `${trim(n / 1e7)}Cr`;
  if (a >= 1e5) return `${trim(n / 1e5)}L`;
  if (a >= 1e3) return `${trim(n / 1e3)}K`;
  return trim(n);
};

export const pct = (n, digits = 0) => `${Number(n.toFixed(digits))}%`;
export const times = (n) => `${n.toFixed(2)}×`;
export const yrs = (n) => `${n} yr${n === 1 ? '' : 's'}`;

// "year 7, month 4" from a 1-based month count.
export const depletedAt = (month) => `year ${Math.ceil(month / 12)}, month ${((month - 1) % 12) + 1}`;
