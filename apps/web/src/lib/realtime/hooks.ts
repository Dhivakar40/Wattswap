"use client";

import * as React from "react";
import { ApiError } from "@/lib/api/client";
import { useConnectionStore } from "@/lib/store/connection-store";
import type { ConnectionStatus, DataFrame, Snapshot } from "./client";
import { useRealtimeClient } from "./provider";

export interface UseChannelOptions<S> {
  /** Channel name, or null to stay unsubscribed (e.g. signed out). */
  name: string | null;
  loadSnapshot(): Promise<Snapshot<S>>;
  apply(state: S, frame: DataFrame): S;
  /** Each live delta that was applied (not ones folded into a snapshot). */
  onApplied?(frame: DataFrame): void;
}

export interface ChannelResult<S> {
  state: S | null;
  error: unknown;
  /** True until the first snapshot is installed (and again while re-syncing from scratch). */
  loading: boolean;
}

/** Subscribe to one channel with snapshot+delta consistency; returns its live state. */
export function useChannel<S>(options: UseChannelOptions<S>): ChannelResult<S> {
  const client = useRealtimeClient();
  const [state, setState] = React.useState<S | null>(null);
  const [error, setError] = React.useState<unknown>(null);
  const latest = React.useRef(options);
  latest.current = options;
  const { name } = options;

  React.useEffect(() => {
    setState(null);
    setError(null);
    if (client === null || name === null) return;
    const sub = client.subscribe<S>({
      name,
      loadSnapshot: () => latest.current.loadSnapshot(),
      apply: (s, f) => latest.current.apply(s, f),
      onState: (s) => {
        setError(null);
        setState(s);
      },
      onApplied: (f) => latest.current.onApplied?.(f),
      onError: (e) => setError(e),
      // A 401 is the session ending: the session layer handles it; retrying cannot fix it.
      shouldRetry: (e) => !(e instanceof ApiError && (e.status === 401 || e.status === 403)),
    });
    return () => sub.unsubscribe();
  }, [client, name]);

  return { state, error, loading: name !== null && state === null && error === null };
}

export function useConnectionStatus(): ConnectionStatus {
  return useConnectionStore((s) => s.status);
}

/** A debounced trigger: many calls inside `ms` collapse into one trailing call. */
export function useDebouncedCallback(fn: () => void, ms: number): () => void {
  const ref = React.useRef(fn);
  ref.current = fn;
  const timer = React.useRef<ReturnType<typeof setTimeout> | null>(null);
  React.useEffect(
    () => () => {
      if (timer.current !== null) clearTimeout(timer.current);
    },
    [],
  );
  return React.useCallback(() => {
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      timer.current = null;
      ref.current();
    }, ms);
  }, [ms]);
}
