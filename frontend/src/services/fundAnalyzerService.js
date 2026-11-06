import axiosClient from './axiosClient.js';

export async function searchFunds(query) {
  const { data } = await axiosClient.get('/search', { params: { q: query } });
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

// portfolioMonth (YYYY-MM) asks for historical overlap: every fund is compared on that one month.
export async function getHoldingsOverlap(schemeCodes, portfolioMonth) {
  const { data } = await axiosClient.post('/overlap', { scheme_codes: schemeCodes, ...(portfolioMonth ? { portfolio_month: portfolioMonth } : {}) });
  return data;
}

// A job answered from saved holdings is done in a fraction of a second, so ask soon, then slow down to once a second.
const FIRST_POLL_MS = 150;
const MAX_POLL_MS = 1000;
const POLL_LIMIT_MS = 5 * 60 * 1000;
const pause = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });

// Starts the overlap as a job and polls it, so a slow first retrieval never blocks the page.
// onStage receives the server's own stage text. isCancelled stops polling once the answer is not wanted.
// If the server has no job endpoint, falls back to the single request.
export async function getHoldingsOverlapWithProgress(schemeCodes, portfolioMonth, onStage = () => {}, isCancelled = () => false) {
  let job;
  try {
    const { data } = await axiosClient.post('/overlap/jobs', { scheme_codes: schemeCodes, ...(portfolioMonth ? { portfolio_month: portfolioMonth } : {}) });
    job = data;
  } catch (error) {
    if (error.response?.status === 404 || error.response?.status === 405) return getHoldingsOverlap(schemeCodes, portfolioMonth);
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
      if (error.response?.status === 404) throw new Error('The overlap request expired. Press Update analysis again.');
      throw error;
    }
  }
  return null;
}

export async function getFund(schemeCode) {
  const { data } = await axiosClient.get(`/funds/${encodeURIComponent(schemeCode)}`);
  return data;
}
