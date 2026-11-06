import useCalculation from '../useCalculation.js';
import CalcPanel from '../components/CalcPanel.jsx';
import ChartCard, { NAVY } from '../components/ChartCard.jsx';
import { Breakdown, ResultCard, ResultRow } from '../components/Results.jsx';
import { getSipLumpsumSwp } from '../../../services/calculatorService.js';
import { depletedAt, inr, pct, yrs } from '../format.js';

export const defaults = {
  monthly: '20000', lumpsum: '500000', returnRate: '12', years: '15',
  withdrawal: '60000', swpReturn: '9', swpYears: '20',
};

const SECTIONS = [
  {
    heading: 'ACCUMULATION PHASE',
    fields: [
      { name: 'monthly', label: 'Monthly SIP', kind: 'money' },
      { name: 'lumpsum', label: 'Initial lumpsum', kind: 'money' },
      { name: 'returnRate', label: 'Return', kind: 'percent' },
      { name: 'years', label: 'Years', kind: 'years' },
    ],
  },
  {
    heading: 'WITHDRAWAL PHASE',
    fields: [
      { name: 'withdrawal', label: 'Monthly withdrawal', kind: 'money' },
      { name: 'swpReturn', label: 'Return', kind: 'percent' },
      { name: 'swpYears', label: 'Years', kind: 'years' },
    ],
  },
];

export default function SipLumpsumSwpCalculator({ values, onChange }) {
  const { result, error, loading } = useCalculation('siplumpsumswp', getSipLumpsumSwp, values, !(Object.values(values).some((v) => v === '')));

  return (
    <div className="calc__grid">
      <CalcPanel
        title="SIP + lumpsum + SWP"
        description="Build a corpus, then draw a monthly income from it."
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
              label="CORPUS AT RETIREMENT"
              value={inr(result.corpus_at_retirement)}
              note={`after ${yrs(result.years)} of SIP + lumpsum at ${pct(result.return_rate, 2)}`}
            />
            <ResultRow>
              <ResultCard
                tone="accent"
                label="TOTAL WITHDRAWN"
                value={inr(result.total_withdrawn)}
                note={yrs(result.withdrawal_years)}
              />
              <ResultCard
                label="ENDING BALANCE"
                value={inr(result.ending_balance)}
                valueClass={result.depleted ? 'calc-negative' : 'calc-positive'}
                note={result.depleted
                  ? `Runs out in withdrawal ${depletedAt(result.depletion_month)}`
                  : 'Left after withdrawals'}
              />
            </ResultRow>
            <Breakdown
              rows={[
                { label: 'Invested (SIP + lumpsum)', value: inr(result.total_invested) },
                { label: 'Corpus at end of accumulation', value: inr(result.corpus_at_retirement) },
                { label: 'Withdrawals', value: inr(result.total_withdrawn) },
              ]}
              total={{ label: 'Ending balance', value: inr(result.ending_balance) }}
            />
          </div>
        )}
      </CalcPanel>

      {result && (
        <ChartCard
          title="Accumulation → withdrawal journey"
          subtitle={`Build for ${yrs(result.years)}, then withdraw for ${yrs(result.withdrawal_years)}`}
          data={result.chart}
          marker={{ x: result.years, label: 'Withdrawals start' }}
          series={[{ key: 'corpus', label: 'Corpus', color: NAVY, fill: true }]}
          stats={[
            { label: 'Peak corpus', value: inr(result.peak_corpus) },
            { label: 'Sustainable withdrawal', value: `${inr(result.sustainable_withdrawal)}/mo` },
          ]}
        />
      )}
    </div>
  );
}
