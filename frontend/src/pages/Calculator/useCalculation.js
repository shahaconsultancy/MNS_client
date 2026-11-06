import { useEffect, useRef, useState } from 'react';

// Results already calculated in this browser session, so switching back to a calculator shows them at once.
const results = new Map();
const MAX_RESULTS = 60;
const TYPING_PAUSE_MS = 400;

function remember(key, data) {
  results.delete(key);
  results.set(key, data);
  if (results.size > MAX_RESULTS) results.delete(results.keys().next().value);
}

/**
 * Calculates on the server and keeps the answer on screen.
 *
 * When nothing is shown yet (the calculator was just opened) it asks straight away. While the user is
 * editing a value it waits for a short pause in typing, so it does not send a request per keystroke.
 */
export default function useCalculation(name, fetcher, values, ready) {
  const key = `${name}:${JSON.stringify(values)}`;
  const [state, setState] = useState(() => ({ result: results.get(key) ?? null, error: '', loading: false }));
  const shown = useRef(state.result !== null);

  useEffect(() => {
    if (!ready) {
      setState((current) => ({ ...current, error: 'Fill in all fields to calculate.', loading: false }));
      return undefined;
    }
    if (results.has(key)) {
      shown.current = true;
      setState({ result: results.get(key), error: '', loading: false });
      return undefined;
    }

    const controller = new AbortController();
    const timer = setTimeout(() => {
      setState((current) => ({ ...current, loading: true }));
      fetcher(values, controller.signal)
        .then((data) => {
          remember(key, data);
          shown.current = true;
          setState({ result: data, error: '', loading: false });
        })
        .catch((err) => {
          if (!controller.signal.aborted) setState((current) => ({ ...current, error: err.message, loading: false }));
        });
    }, shown.current ? TYPING_PAUSE_MS : 0);

    // Inputs changed again, or the calculator closed: drop the pending timer and request.
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [key, ready]); // eslint-disable-line react-hooks/exhaustive-deps -- key already covers every value

  return state;
}
