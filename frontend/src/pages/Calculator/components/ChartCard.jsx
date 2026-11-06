import {
  Area, CartesianGrid, ComposedChart, Line, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts';
import { compact, inr } from '../format.js';

export const NAVY = '#1b2a5c';
export const GOLD = '#c29035';

// About eight evenly spaced year ticks, always ending on the last year.
const yearTicks = (last) => {
  const step = Math.max(1, Math.ceil(last / 8));
  const ticks = [];
  for (let y = 0; y < last; y += step) ticks.push(y);
  ticks.push(last);
  return ticks;
};

function ChartTooltip({ active, payload, label, series }) {
  if (!active || !payload?.length) return null;
  return (
    <div className="calc-tooltip">
      <span className="calc-tooltip__year">Year {label}</span>
      {series.map((s) => {
        const p = payload.find((x) => x.dataKey === s.key);
        return p ? (
          <span key={s.key} className="calc-tooltip__row">
            <i style={{ background: s.color }} /> {s.label} <strong>{inr(p.value)}</strong>
          </span>
        ) : null;
      })}
    </div>
  );
}

/*
 * series: [{ key, label, color, dashed?, fill? }]
 * stats:  [{ label, value }] shown under the chart
 * marker: { x, label } vertical reference line
 */
export default function ChartCard({ title, subtitle, data, series, stats = [], marker }) {
  const last = data.length ? data[data.length - 1].year : 0;

  return (
    <section className="calc-card calc-chart">
      <header className="calc-chart__head">
        <div>
          <h2 className="calc-card__title">{title}</h2>
          <p className="calc-chart__sub">{subtitle}</p>
        </div>
        <ul className="calc-chart__legend">
          {series.map((s) => (
            <li key={s.key}>
              <i style={{ borderTop: `2px ${s.dashed ? 'dashed' : 'solid'} ${s.color}` }} /> {s.label}
            </li>
          ))}
        </ul>
      </header>

      <div className="calc-chart__plot">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={data} margin={{ top: 16, right: 12, bottom: 18, left: 0 }}>
            <defs>
              {series.filter((s) => s.fill).map((s) => (
                <linearGradient key={s.key} id={`fill-${s.key}`} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={s.color} stopOpacity={0.14} />
                  <stop offset="100%" stopColor={s.color} stopOpacity={0.08} />
                </linearGradient>
              ))}
            </defs>
            <CartesianGrid vertical={false} stroke="#ece8de" strokeDasharray="3 3" />
            <XAxis
              dataKey="year"
              type="number"
              domain={[0, last]}
              ticks={yearTicks(last)}
              tick={{ fontSize: 11, fill: '#9a9a9a' }}
              tickLine={false}
              axisLine={{ stroke: '#e3ded2' }}
              label={{ value: 'Years', position: 'insideBottom', offset: -12, fontSize: 11, fill: '#9a9a9a' }}
            />
            <YAxis
              domain={[0, 'auto']}
              tickFormatter={compact}
              tick={{ fontSize: 11, fill: '#9a9a9a' }}
              tickLine={false}
              axisLine={false}
              width={52}
            />
            <Tooltip
              content={<ChartTooltip series={series} />}
              cursor={{ stroke: '#9aa0b4', strokeWidth: 1 }}
            />
            {marker && (
              <ReferenceLine
                x={marker.x}
                stroke={GOLD}
                strokeDasharray="4 4"
                label={{ value: marker.label, position: 'insideTopRight', fontSize: 11, fontWeight: 600, fill: '#8a6420' }}
              />
            )}
            {series.map((s) =>
              s.fill ? (
                <Area
                  key={s.key}
                  type="monotone"
                  dataKey={s.key}
                  stroke={s.color}
                  strokeWidth={2.2}
                  fill={`url(#fill-${s.key})`}
                  dot={false}
                  activeDot={{ r: 4 }}
                  isAnimationActive={false}
                />
              ) : (
                <Line
                  key={s.key}
                  type="monotone"
                  dataKey={s.key}
                  stroke={s.color}
                  strokeWidth={2}
                  strokeDasharray={s.dashed ? '6 5' : undefined}
                  dot={false}
                  activeDot={{ r: 4 }}
                  isAnimationActive={false}
                />
              ),
            )}
          </ComposedChart>
        </ResponsiveContainer>
      </div>

      {stats.length > 0 && (
        <footer className="calc-chart__stats">
          {stats.map((s) => (
            <span key={s.label}>{s.label} <strong>{s.value}</strong></span>
          ))}
        </footer>
      )}
    </section>
  );
}
