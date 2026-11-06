import useCalculation from '../useCalculation.js';
import CalcPanel from '../components/CalcPanel.jsx';
import ChartCard, { GOLD, NAVY } from '../components/ChartCard.jsx';
import { Breakdown, ResultCard, ResultRow } from '../components/Results.jsx';
import { getSip } from '../../../services/calculatorService.js';
import { inr, pct, times, yrs } from '../format.js';

export const defaults = { monthly: '25000', returnRate: '12', years: '15' };

const SECTIONS = [{
  heading: 'INPUTS',
  fields: [
    { name: 'monthly', label: 'Monthly SIP', kind: 'money' },
    { name: 'returnRate', label: 'Expected return', kind: 'percent' },
    { name: 'years', label: 'Years', kind: 'years' },
  ],
}];

export default function SipCalculator({ values, onChange }) {
  const { result, error, loading } = useCalculation('sip', getSip, values, !(!values.monthly || !values.returnRate || !values.years));

  return (
    <div className="calc__grid">
      <CalcPanel
        title="SIP calculator"
        description="What a fixed monthly investment grows to."
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
              label="MATURITY VALUE"
              value={inr(result.maturity_value)}
              note={`${inr(result.monthly_sip)}/mo for ${yrs(result.years)} at ${pct(result.return_rate, 2)}`}
            />
            <ResultRow>
              <ResultCard label="INVESTED" value={inr(result.total_invested)} />
              <ResultCard tone="accent" label="EST. GAINS" value={inr(result.gains)} />
            </ResultRow>
            <Breakdown
              rows={[
                { label: 'Monthly SIP', value: inr(result.monthly_sip) },
                { label: 'Instalments', value: `${result.months} months` },
                { label: 'Total invested', value: inr(result.total_invested) },
                { label: 'Est. gains', value: inr(result.gains) },
              ]}
              total={{ label: 'Maturity value', value: inr(result.maturity_value) }}
            />
          </div>
        )}
      </CalcPanel>

      {result && (
        <ChartCard
          title="SIP wealth growth"
          subtitle={`Gains are ${pct(result.gains_percent_of_invested)} of the amount invested`}
          data={result.chart}
          series={[
            { key: 'value', label: 'Value', color: NAVY, fill: true },
            { key: 'invested', label: 'Invested', color: GOLD, dashed: true },
          ]}
          stats={[
            { label: 'Wealth multiple', value: times(result.wealth_multiple) },
            { label: 'Gains share of value', value: pct(result.gains_share_percent, 1) },
          ]}
        />
      )}
    </div>
  );
}
