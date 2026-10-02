"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";

/** Ambil `path` berkala. `intervalMs` boleh berubah menurut data terakhir. */
export function usePoll<T>(path: string, intervalMs: (data: T | null) => number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const dataRef = useRef<T | null>(null);
  const pathRef = useRef(path);
  pathRef.current = path;

  const load = useCallback(async () => {
    try {
      const d = await api<T>(pathRef.current);
      dataRef.current = d;
      setData(d);
      setError(null);
      return d;
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
      return null;
    }
  }, []);

  const iv = intervalMs(data);
  useEffect(() => {
    load();
    const t = setInterval(load, iv);
    return () => clearInterval(t);
  }, [load, iv]);

  return { data, error, reload: load, setData };
}
