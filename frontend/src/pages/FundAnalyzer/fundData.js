const PALETTE = ['#2f6fde', '#ea5b2a', '#1f8a4c', '#8a4fd3', '#0f9aa8', '#d6407f', '#b7791f', '#475569'];

// Any number of funds can be selected. The first few use the fixed palette; after that each
// fund gets its own hue, spaced by the golden angle so neighbours stay easy to tell apart.
export const fundColor = (index) => (index < PALETTE.length ? PALETTE[index] : `hsl(${Math.round((index * 137.508) % 360)}, 62%, 42%)`);
