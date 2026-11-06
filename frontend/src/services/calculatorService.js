import axiosClient from './axiosClient.js';

// `signal` lets the caller cancel a request whose inputs are already out of date.
export async function getInflation({ presentCost, rate, years }, signal) {
  const { data } = await axiosClient.get('/calculators/inflation/', {
    params: { present_cost: presentCost, rate, years },
    signal,
  });
  return data;
}

export async function getGoal({ goalCost, inflation, years, returnRate, savings }, signal) {
  const { data } = await axiosClient.get('/calculators/goal/', {
    params: { goal_cost: goalCost, inflation, years, return_rate: returnRate, savings },
    signal,
  });
  return data;
}

export async function getSip({ monthly, returnRate, years }, signal) {
  const { data } = await axiosClient.get('/calculators/sip/', {
    params: { monthly_sip: monthly, return_rate: returnRate, years },
    signal,
  });
  return data;
}

export async function getSwp({ corpus, withdrawal, returnRate, years }, signal) {
  const { data } = await axiosClient.get('/calculators/swp/', {
    params: { corpus, monthly_withdrawal: withdrawal, return_rate: returnRate, years },
    signal,
  });
  return data;
}

export async function getSipLumpsum({ monthly, lumpsum, returnRate, years }, signal) {
  const { data } = await axiosClient.get('/calculators/sip-lumpsum/', {
    params: { monthly_sip: monthly, lumpsum, return_rate: returnRate, years },
    signal,
  });
  return data;
}

export async function getSipLumpsumSwp(values, signal) {
  const { monthly, lumpsum, returnRate, years, withdrawal, swpReturn, swpYears } = values;
  const { data } = await axiosClient.get('/calculators/sip-lumpsum-swp/', {
    params: {
      monthly_sip: monthly,
      lumpsum,
      return_rate: returnRate,
      years,
      monthly_withdrawal: withdrawal,
      withdrawal_return_rate: swpReturn,
      withdrawal_years: swpYears,
    },
    signal,
  });
  return data;
}
