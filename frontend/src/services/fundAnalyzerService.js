import axiosClient from './axiosClient.js';

export async function searchFunds(query, { offset = 0, limit = 50 } = {}) {
  const { data } = await axiosClient.get('/search', { params: { q: query, offset, limit } });
  return data;
}

export async function compareFunds(schemeCodes) {
  const { data } = await axiosClient.post('/compare', { scheme_codes: schemeCodes });
  return data;
}

export async function getFundamentals(schemeCodes) {
  const { data } = await axiosClient.post('/fundamentals', { scheme_codes: schemeCodes });
  return data;
}

// Large selections are loaded in small groups, so no single request runs long enough to hit a hosting time limit
// (Vercel stops a function after its maximum duration). Every fund is calculated on its own, so the combined result
// is the same as one request for all of them. A group that fails is asked again once; if it still fails, only its
// funds are shown as unavailable and every other fund is shown normally.
const BATCH_SIZE = 6;
const PARALLEL_BATCHES = 2;
export const NAV_UNAVAILABLE_NOTE = 'NAV history could not be loaded right now, so returns and risk are not shown. Press Update analysis again in a moment.';

async function runInBatches(items, run) {
  const batches = [];
  for (let i = 0; i < items.length; i += BATCH_SIZE) batches.push(items.slice(i, i + BATCH_SIZE));
  const results = new Array(batches.length);
  let next = 0;
  const worker = async () => {
    while (next < batches.length) {
      const index = next;
      next += 1;
      try {
        results[index] = { value: await run(batches[index]) };
      } catch {
        try {
          results[index] = { value: await run(batches[index]) };
        } catch (error) {
          results[index] = { error };
        }
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(PARALLEL_BATCHES, batches.length) }, worker));
  return { batches, results };
}

// selectedFunds: the picked funds ({ scheme_code, scheme_name, amc, ... }), used to name a fund whose group failed.
export async function compareFundsInBatches(selectedFunds) {
  const codes = selectedFunds.map((fund) => fund.scheme_code);
  const { results } = await runInBatches(codes, (batch) => compareFunds(batch));
  const loaded = results.filter((result) => result.value);
  if (!loaded.length) throw results[0].error;
  const byCode = {};
  loaded.forEach(({ value }) => (value.funds || []).forEach((fund) => { byCode[fund.scheme_code] = fund; }));
  const picked = Object.fromEntries(selectedFunds.map((fund) => [fund.scheme_code, fund]));
  const funds = codes.map((code) => byCode[code] || {
    scheme_code: code,
    scheme_name: picked[code]?.scheme_name || picked[code]?.display_name || code,
    amc: picked[code]?.amc || null,
    category: picked[code]?.category || null,
    plan: picked[code]?.plan || null,
    option: picked[code]?.option || null,
    source: 'AMFI',
    history: [],
    metrics: null,
    metrics_unavailable: NAV_UNAVAILABLE_NOTE,
  });
  const dates = loaded.map(({ value }) => value.asOfDate).filter(Boolean).sort();
  return {
    funds,
    source: loaded.some(({ value }) => value.source === 'MFAPI (backup)') ? 'MFAPI (backup)' : (loaded[0].value.source || 'AMFI'),
    asOfDate: dates.length ? dates[dates.length - 1] : null,
    summary: loaded[0].value.summary || null,
  };
}

// Fundamentals for many funds, in groups. A fund whose group failed comes back as { scheme_code, unavailable: true }.
export async function getFundamentalsInBatches(schemeCodes) {
  const { batches, results } = await runInBatches(schemeCodes, (batch) => getFundamentals(batch));
  if (results.every((result) => result.error)) throw results[0].error;
  const funds = [];
  results.forEach((result, index) => {
    if (result.value) funds.push(...(result.value.funds || []));
    else funds.push(...batches[index].map((code) => ({ scheme_code: code, unavailable: true })));
  });
  return { funds };
}

// The single-request overlap does all its work before it answers, so it may take a while on a first
// retrieval. Past this the browser stops waiting; the server's own limit may end it sooner (HTTP 504).
const SYNC_OVERLAP_TIMEOUT_MS = 5 * 60 * 1000;
export const OVERLAP_TIMEOUT_MESSAGE = 'Calculating the overlap took longer than the server allows. Press Update analysis again; holdings already retrieved are reused.';

// portfolioMonth (YYYY-MM) asks for historical overlap: every fund is compared on that one month.
// A time-out gets its own message. Any other failure keeps the server's own reason.
export async function getHoldingsOverlap(schemeCodes, portfolioMonth) {
  try {
    const { data } = await axiosClient.post('/overlap', { scheme_codes: schemeCodes, ...(portfolioMonth ? { portfolio_month: portfolioMonth } : {}) }, { timeout: SYNC_OVERLAP_TIMEOUT_MS });
    return data;
  } catch (error) {
    if (error.code === 'ECONNABORTED' || error.response?.status === 504) throw new Error(OVERLAP_TIMEOUT_MESSAGE);
    throw error;
  }
}

// A job answered from saved holdings is done in a fraction of a second, so ask soon, then slow down to once a second.
const FIRST_POLL_MS = 150;
const MAX_POLL_MS = 1000;
const POLL_LIMIT_MS = 5 * 60 * 1000;
const pause = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });

// Starts the overlap as a job and polls it, so a slow first retrieval never blocks the page.
// onStage receives the server's own stage text. isCancelled stops polling once the answer is not wanted.
// Where the server runs no jobs (405, as on Vercel) the single request is used instead. If a running job
// goes missing (404: on serverless hosting the instance that held it can be gone), the single request is
// tried once. It never comes back here, so there is no retry loop.
export async function getHoldingsOverlapWithProgress(schemeCodes, portfolioMonth, onStage = () => {}, isCancelled = () => false) {
  const synchronous = () => { onStage('Calculating overlap...'); return getHoldingsOverlap(schemeCodes, portfolioMonth); };
  let job;
  try {
    const { data } = await axiosClient.post('/overlap/jobs', { scheme_codes: schemeCodes, ...(portfolioMonth ? { portfolio_month: portfolioMonth } : {}) });
    job = data;
  } catch (error) {
    if (error.response?.status === 404 || error.response?.status === 405) return synchronous();
    throw error;
  }
  const started = Date.now();
  let wait = FIRST_POLL_MS;
  while (!isCancelled()) {
    if (job.status === 'done') return job.result;
    if (job.status === 'failed') throw new Error(job.error || 'Overlap could not be calculated');
    onStage(job.message);
    if (Date.now() - started > POLL_LIMIT_MS) throw new Error('Retrieving portfolio holdings is taking too long. Try again shortly.');
    await pause(wait);
    wait = Math.min(MAX_POLL_MS, Math.round(wait * 1.6));
    if (isCancelled()) break;
    try {
      ({ data: job } = await axiosClient.get(`/overlap/jobs/${job.job_id}`));
    } catch (error) {
      if (error.response?.status === 404) return isCancelled() ? null : synchronous();
      throw error;
    }
  }
  return null;
}

export async function getFund(schemeCode) {
  const { data } = await axiosClient.get(`/funds/${encodeURIComponent(schemeCode)}`);
  return data;
}
