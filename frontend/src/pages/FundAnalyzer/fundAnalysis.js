export const signedPct = (n) => n == null ? '—' : `${n > 0 ? '+' : n < 0 ? '−' : ''}${Math.abs(n * 100).toFixed(2)}%`;
export const nav = (n) => n == null ? '—' : `₹${Number(n).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 4 })}`;
export const pct = (n) => n == null ? '—' : `${Number(n * 100).toFixed(2)}%`;
// AMFI reports AAUM in lakhs; 100 lakhs is 1 crore.
export const crore = (lakhs) => lakhs == null ? '—' : `₹${Number(lakhs / 100).toLocaleString('en-IN', { maximumFractionDigits: 0 })} Cr`;
