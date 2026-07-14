"use client";

import { useCallback, useEffect, useState } from "react";

// Tiny data-loading hook for the dashboard.
// `deps` re-runs the fetch (e.g. when a route param changes); `reload()` re-runs it on
// demand (e.g. after a write mutates state) without changing deps.
//
// SELF-HEALING: while in an error state (typically the api restarting on a deploy), the
// hook silently retries every 8s while the tab is visible — the moment the backend is back,
// data loads and the error card disappears without the operator touching anything. Combined
// with the transient-retry inside api.get(), a deploy window never needs a manual Refresh.
const _RECOVER_INTERVAL_MS = 8000;

export function useApi<T>(fn: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);

  const reload = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    fn()
      .then((d) => alive && setData(d))
      .catch((e) => alive && setError(e instanceof Error ? e.message : String(e)))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);

  useEffect(() => {
    if (!error) return;
    // Retry even in background tabs — the requests are tiny, and the app must already be
    // healed by the time the operator looks at it again.
    const id = setInterval(() => setTick((t) => t + 1), _RECOVER_INTERVAL_MS);
    return () => clearInterval(id);
  }, [error]);

  return { data, error, loading, reload };
}
