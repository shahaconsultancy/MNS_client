from __future__ import annotations

from typing import Iterable

import numpy as np
import pandas as pd


def _series(nav: Iterable[dict]) -> pd.Series:
    frame = pd.DataFrame(list(nav))
    if frame.empty:
        return pd.Series(dtype=float)
    frame['date'] = pd.to_datetime(frame['date'], errors='coerce')
    frame['nav'] = pd.to_numeric(frame['nav'], errors='coerce')
    frame = frame.dropna(subset=['date', 'nav']).drop_duplicates('date').sort_values('date')
    return frame.set_index('date')['nav']


def nearest_on_or_before(series: pd.Series, target: pd.Timestamp) -> float | None:
    if series.empty:
        return None
    earlier = series.loc[series.index <= target]
    return float(earlier.iloc[-1]) if not earlier.empty else None


def trailing_return(nav: Iterable[dict], months: int) -> float | None:
    series = _series(nav)
    if series.empty:
        return None
    end = float(series.iloc[-1])
    start = nearest_on_or_before(series, series.index[-1] - pd.DateOffset(months=months))
    return None if start in (None, 0) else end / start - 1


def cagr(nav: Iterable[dict], years: int) -> float | None:
    series = _series(nav)
    if series.empty:
        return None
    end = float(series.iloc[-1])
    start = nearest_on_or_before(series, series.index[-1] - pd.DateOffset(years=years))
    return None if start in (None, 0) else (end / start) ** (1 / years) - 1


def daily_returns(nav: Iterable[dict]) -> pd.Series:
    series = _series(nav)
    return series.pct_change().dropna()


def volatility(nav: Iterable[dict], years: int = 3) -> float | None:
    series = _series(nav)
    if series.empty:
        return None
    cutoff = series.index[-1] - pd.DateOffset(years=years)
    values = series.loc[series.index >= cutoff]
    returns = values.pct_change().dropna()
    return None if returns.empty else float(returns.std(ddof=1) * np.sqrt(252))


def sharpe(nav: Iterable[dict], risk_free_rate: float = 0.065) -> float | None:
    annual_return = cagr(nav, 3)
    annual_volatility = volatility(nav, 3)
    if annual_return is None or annual_volatility in (None, 0):
        return None
    return float((annual_return - risk_free_rate) / annual_volatility)


def max_drawdown(nav: Iterable[dict], years: int = 3) -> float | None:
    series = _series(nav)
    if series.empty:
        return None
    cutoff = series.index[-1] - pd.DateOffset(years=years)
    values = series.loc[series.index >= cutoff]
    if values.empty:
        return None
    drawdown = values / values.cummax() - 1
    return float(drawdown.min())


def correlation(nav_a: Iterable[dict], nav_b: Iterable[dict]) -> float | None:
    a = _series(nav_a).pct_change().rename('a')
    b = _series(nav_b).pct_change().rename('b')
    joined = pd.concat([a, b], axis=1).dropna()
    return None if len(joined) < 2 else float(joined['a'].corr(joined['b']))


def rolling_returns(nav: Iterable[dict], window_days: int = 252) -> list[dict]:
    series = _series(nav)
    if len(series) <= window_days:
        return []
    values = series / series.shift(window_days) - 1
    return [{'date': index.date().isoformat(), 'return': float(value)} for index, value in values.dropna().items()]


def calendar_year_returns(nav: Iterable[dict]) -> list[dict]:
    series = _series(nav)
    if series.empty:
        return []
    result = []
    for year, values in series.groupby(series.index.year):
        if len(values) < 2:
            continue
        result.append({'year': int(year), 'return': float(values.iloc[-1] / values.iloc[0] - 1)})
    return result


def growth_of_10000(nav: Iterable[dict]) -> list[dict]:
    series = _series(nav)
    if series.empty:
        return []
    base = float(series.iloc[0])
    return [{'date': index.date().isoformat(), 'value': float(10000 * value / base)} for index, value in series.items()]


def sip_return(nav: Iterable[dict], monthly_amount: float = 10000) -> dict | None:
    series = _series(nav)
    if series.empty or monthly_amount <= 0:
        return None
    monthly = series.resample('ME').last().dropna()
    if monthly.empty:
        return None
    units = (monthly_amount / monthly).sum()
    invested = monthly_amount * len(monthly)
    value = float(units * monthly.iloc[-1])
    return {'monthlyAmount': monthly_amount, 'months': int(len(monthly)), 'invested': float(invested), 'value': value, 'return': value / invested - 1}


def metrics(nav: Iterable[dict], risk_free_rate: float = 0.065) -> dict:
    nav_rows = list(nav)
    return {
        'r1m': trailing_return(nav_rows, 1), 'r3m': trailing_return(nav_rows, 3),
        'r6m': trailing_return(nav_rows, 6), 'r1y': trailing_return(nav_rows, 12),
        'r3y': cagr(nav_rows, 3), 'r5y': cagr(nav_rows, 5),
        'volatility': volatility(nav_rows), 'sharpe': sharpe(nav_rows, risk_free_rate),
        'maxDrawdown': max_drawdown(nav_rows), 'rollingReturns': rolling_returns(nav_rows),
        'calendarYearReturns': calendar_year_returns(nav_rows), 'growthOf10000': growth_of_10000(nav_rows),
        'sip': sip_return(nav_rows),
    }
