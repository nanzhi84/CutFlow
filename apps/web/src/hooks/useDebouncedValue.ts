import { useEffect, useState } from "react";

/**
 * Return a copy of `value` that only updates after it has stayed unchanged for
 * `delayMs`. Useful to keep a fast-changing value (e.g. an estimate derived
 * from every keystroke) out of a react-query `queryKey` until it settles.
 */
export function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delayMs);
    return () => window.clearTimeout(timer);
  }, [value, delayMs]);
  return debounced;
}
