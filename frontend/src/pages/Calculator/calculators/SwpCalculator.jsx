import useCalculation from '../useCalculation.js';
import CalcPanel from '../components/CalcPanel.jsx';
import ChartCard, { NAVY } from '../components/ChartCard.jsx';
import { Breakdown, ResultCard } from '../components/Results.jsx';
import { getSwp } from '../../../services/calculatorService.js';
import { depletedAt, inr, pct, times, yrs } from '../format.js';

export const defaults = { corpus: '5000000', withdrawal: '30000', returnRate: '9', years: '20' };

const SECTIONS = [{
  heading: 'INPUTS',
  fields: [
    { name: 'corpus', label: 'Initial corpus', kind: 'money' },
    { name: 'withdrawal', label: 'Monthly withdrawal', kind: 'money' },
    { name: 'returnRate', label: 'Expected return', kind: 'percent' },
    { name: 'years', label: 'Years', kind: 'years' },
  ],
}];

export default function SwpCalculator({ values, onChange }) {
  const { result, error, loading } = useCalculation('swp', getSwp, values, !(!values.corpus || !values.withdrawal || !values.returnRate || !values.years));

  return (
    <div className="calc__grid">
      <CalcPanel
        title="SWP calculator"
        description="How a corpus holds up under a fixed monthly withdrawal."
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
              label="TOTAL WITHDRAWN"
              value={inr(result.total_withdrawn)}
              note={`${inr(result.monthly_withdrawal)}/mo for ${yrs(result.years)}`}
            />
            <ResultCard
              tone="accent"
              label="ENDING BALANCE"
              value={inr(result.ending_balance)}
              valueClass={result.depleted ? 'calc-negative' : ''}
              note={result.depleted
                ? `Corpus runs out in ${depletedAt(result.depletion_month)}`
                : `${times(result.ending_multiple)} the starting corpus`}
            />
            <Breakdown
              rows={[
                { label: 'Initial corpus', value: inr(result.corpus) },
                { label: 'Annual withdrawal', value: inr(result.annual_withdrawal) },
                { label: 'Withdrawal rate', value: `${pct(result.withdrawal_rate, 1)} a year` },
              ]}
              total={{ label: 'Ending balance', value: inr(result.ending_balance) }}
            />
          </div>
        )}
      </CalcPanel>

      {result && (
        <ChartCard
          title="Corpus balance during withdrawals"
          subtitle={result.depleted
            ? `Corpus runs out in ${depletedAt(result.depletion_month)}`
            : `Balance after ${yrs(result.years)}: ${inr(result.ending_balance)}`}
          data={result.chart}
          series={[{ key: 'balance', label: 'Balance', color: NAVY, fill: true }]}
          stats={[{ label: 'Sustainable withdrawal (keeps corpus intact)', value: `${inr(result.sustainable_withdrawal)}/mo` }]}
        />
      )}
    </div>
  );
}
