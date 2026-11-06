import useCalculation from '../useCalculation.js';
import CalcPanel from '../components/CalcPanel.jsx';
import ChartCard, { GOLD } from '../components/ChartCard.jsx';
import { Breakdown, ResultCard, ResultRow } from '../components/Results.jsx';
import { getInflation } from '../../../services/calculatorService.js';
import { inr, pct, times, yrs } from '../format.js';

export const defaults = { presentCost: '100000', rate: '6', years: '15' };

const SECTIONS = [{
  heading: 'INPUTS',
  fields: [
    { name: 'presentCost', label: 'Present cost', kind: 'money' },
    { name: 'rate', label: 'Inflation rate', kind: 'percent' },
    { name: 'years', label: 'Years', kind: 'years' },
  ],
}];

export default function InflationCalculator({ values, onChange }) {
  const { result, error, loading } = useCalculation('inflation', getInflation, values, !(!values.presentCost || !values.rate || !values.years));

  return (
    <div className="calc__grid">
      <CalcPanel
        title="Inflation calculator"
        description="What something that costs this much today will cost in the future."
        sections={SECTIONS}
        values={values}
        onChange={onChange}
      >
        {error && <p className="calc-error">{error}</p>}
        {!result && !error && <p className="calc-status">Calculating…</p>}
        {result && (
          <div className={`calc-panel__results-body${loading ? ' calc-loading' : ''}`}>
            <ResultCard
              tone="hero"
              label="FUTURE COST"
              value={inr(result.future_cost)}
              note={`after ${yrs(result.years)} at ${pct(result.rate, 2)} a year`}
            />
            <ResultRow>
              <ResultCard tone="accent" label="COST INCREASE" value={inr(result.cost_increase)} />
              <ResultCard label="TIMES TODAY'S COST" value={times(result.times_cost)} />
            </ResultRow>
            <Breakdown
              rows={[
                { label: 'Present cost', value: inr(result.present_cost) },
                { label: <>× (1 + {pct(result.rate, 2)})<sup>{result.years}</sup></>, value: result.factor.toFixed(4) },
              ]}
              total={{ label: 'Future cost', value: inr(result.future_cost) }}
            />
          </div>
        )}
      </CalcPanel>

      {result && (
        <ChartCard
          title="Cost growth due to inflation"
          subtitle={`From ${inr(result.present_cost)} today to ${inr(result.future_cost)} in ${yrs(result.years)}`}
          data={result.chart}
          series={[{ key: 'cost', label: 'Cost', color: GOLD, fill: true }]}
          stats={[
            { label: 'Today', value: inr(result.present_cost) },
            { label: `Half-way (yr ${result.half_way_year})`, value: inr(result.half_way_cost) },
            { label: 'Purchasing power of ₹1 lakh then', value: inr(result.purchasing_power_of_lakh) },
          ]}
        />
      )}
    </div>
  );
}
