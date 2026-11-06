import useCalculation from '../useCalculation.js';
import CalcPanel from '../components/CalcPanel.jsx';
import ChartCard, { GOLD, NAVY } from '../components/ChartCard.jsx';
import { Breakdown, ResultCard, ResultRow } from '../components/Results.jsx';
import { getSipLumpsum } from '../../../services/calculatorService.js';
import { inr, pct, times, yrs } from '../format.js';

export const defaults = { monthly: '20000', lumpsum: '500000', returnRate: '12', years: '15' };

const SECTIONS = [{
  heading: 'INPUTS',
  fields: [
    { name: 'monthly', label: 'Monthly SIP', kind: 'money' },
    { name: 'lumpsum', label: 'Initial lumpsum', kind: 'money' },
    { name: 'returnRate', label: 'Expected return', kind: 'percent' },
    { name: 'years', label: 'Years', kind: 'years' },
  ],
}];

export default function SipLumpsumCalculator({ values, onChange }) {
  const { result, error, loading } = useCalculation('siplumpsum', getSipLumpsum, values, !(!values.monthly || !values.lumpsum || !values.returnRate || !values.years));

  return (
    <div className="calc__grid">
      <CalcPanel
        title="SIP + lumpsum calculator"
        description="A one-time investment plus a monthly SIP, grown together."
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
              note={`after ${yrs(result.years)} at ${pct(result.return_rate, 2)}`}
            />
            <ResultRow>
              <ResultCard label="INVESTED" value={inr(result.total_invested)} />
              <ResultCard tone="accent" label="GAINS" value={inr(result.gains)} />
            </ResultRow>
            <Breakdown
              rows={[
                { label: 'SIP grows to', value: inr(result.sip_value) },
                { label: 'Lumpsum grows to', value: inr(result.lumpsum_value) },
              ]}
              total={{ label: 'Maturity value', value: inr(result.maturity_value) }}
            />
          </div>
        )}
      </CalcPanel>

      {result && (
        <ChartCard
          title="SIP + lumpsum growth"
          subtitle={`SIP contributes ${pct(result.sip_share_percent)}, lumpsum ${pct(result.lumpsum_share_percent)} of the final value`}
          data={result.chart}
          series={[
            { key: 'value', label: 'Value', color: NAVY, fill: true },
            { key: 'invested', label: 'Invested', color: GOLD, dashed: true },
          ]}
          stats={[
            { label: 'Wealth multiple', value: times(result.wealth_multiple) },
            { label: 'SIP invested', value: inr(result.sip_invested) },
            { label: 'Lumpsum invested', value: inr(result.lumpsum) },
          ]}
        />
      )}
    </div>
  );
}
