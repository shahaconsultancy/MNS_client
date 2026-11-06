import useCalculation from '../useCalculation.js';
import CalcPanel from '../components/CalcPanel.jsx';
import ChartCard, { GOLD, NAVY } from '../components/ChartCard.jsx';
import { Breakdown, ResultCard } from '../components/Results.jsx';
import { getGoal } from '../../../services/calculatorService.js';
import { inr, pct, yrs } from '../format.js';

export const defaults = { goalCost: '2000000', inflation: '6', years: '10', returnRate: '12', savings: '200000' };

const SECTIONS = [{
  heading: 'INPUTS',
  fields: [
    { name: 'goalCost', label: 'Goal cost (today)', kind: 'money' },
    { name: 'inflation', label: 'Inflation', kind: 'percent' },
    { name: 'years', label: 'Years to goal', kind: 'years' },
    { name: 'returnRate', label: 'Expected return', kind: 'percent' },
    { name: 'savings', label: 'Existing savings', kind: 'money', hint: 'Grows at the expected return' },
  ],
}];

export default function GoalCalculator({ values, onChange }) {
  const { result, error, loading } = useCalculation('goal', getGoal, values, !(!values.goalCost || !values.inflation || !values.years || !values.returnRate || !values.savings));

  const covered = result && result.shortfall === 0;

  return (
    <div className="calc__grid">
      <CalcPanel
        title="Goal-based calculator"
        description="The monthly SIP needed to reach a goal, after inflation and what is already saved."
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
              label="FUTURE GOAL VALUE"
              value={inr(result.future_goal_value)}
              note={`${inr(result.goal_cost)} at ${pct(result.inflation, 2)} inflation for ${yrs(result.years)}`}
            />
            <ResultCard
              tone="accent"
              label="REQUIRED MONTHLY SIP"
              value={inr(result.monthly_sip)}
              note={covered
                ? 'Existing savings already cover this goal'
                : `for ${result.months} months at ${pct(result.return_rate, 2)}`}
            />
            <Breakdown
              rows={[
                { label: 'Total SIP invested', value: inr(result.total_invested) },
                { label: 'Future goal value', value: inr(result.future_goal_value) },
                { label: '− Existing savings, grown', value: inr(result.savings_grown) },
              ]}
              total={{ label: 'Shortfall to fund by SIP', value: inr(result.shortfall) }}
            />
          </div>
        )}
      </CalcPanel>

      {result && (
        <ChartCard
          title="Corpus vs inflated goal"
          subtitle={covered
            ? 'Existing savings reach the goal without a SIP'
            : `SIP of ${inr(result.monthly_sip)}/mo reaches the goal in year ${result.years}`}
          data={result.chart}
          series={[
            { key: 'corpus', label: 'Projected corpus', color: NAVY, fill: true },
            { key: 'goal', label: 'Goal (inflated)', color: GOLD, dashed: true },
          ]}
          stats={[
            { label: 'SIP invested', value: inr(result.total_invested) },
            { label: 'Savings grown', value: inr(result.savings_grown) },
            { label: 'Growth earned', value: inr(Math.max(result.growth_earned, 0)) },
          ]}
        />
      )}
    </div>
  );
}
