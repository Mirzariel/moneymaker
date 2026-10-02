// Semua request same-origin ke /api/... (UI disajikan oleh bot).
// Autentikasi = cookie sesi HttpOnly yang dipasang bot lewat /login?token=...
// Browser tidak pernah melihat token.

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

type Listener = () => void;
const unauthorizedListeners = new Set<Listener>();

/** Dipanggil setiap kali ada respons 401 (sesi tidak valid). Mengembalikan fungsi unsubscribe. */
export function onUnauthorized(cb: Listener): () => void {
  unauthorizedListeners.add(cb);
  return () => {
    unauthorizedListeners.delete(cb);
  };
}

export interface ApiOptions {
  method?: "GET" | "POST";
  body?: unknown;
  timeoutMs?: number;
}

export async function api<T>(path: string, opts?: ApiOptions): Promise<T> {
  const method = opts?.method ?? "GET";
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), opts?.timeoutMs ?? 30_000);
  let res: Response;
  try {
    res = await fetch(`/api/${path}`, {
      method,
      cache: "no-store",
      credentials: "same-origin",
      signal: ctrl.signal,
      headers: method === "POST" ? { "Content-Type": "application/json" } : undefined,
      body: method === "POST" ? JSON.stringify(opts?.body ?? {}) : undefined,
    });
  } catch (e) {
    clearTimeout(timer);
    if (e instanceof DOMException && e.name === "AbortError") {
      throw new ApiError(0, "Permintaan terlalu lama (timeout)");
    }
    throw new ApiError(0, "Tidak bisa menghubungi bot. Pastikan jendela terminal masih terbuka.");
  }
  clearTimeout(timer);
  const text = await res.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    if (data && typeof data === "object" && "detail" in data) {
      const d = (data as { detail: unknown }).detail;
      detail = typeof d === "string" ? d : JSON.stringify(d);
    } else if (typeof data === "string" && data) {
      detail = data.slice(0, 200);
    }
    if (res.status === 401 && path !== "session") unauthorizedListeners.forEach((cb) => cb());
    throw new ApiError(res.status, detail);
  }
  return data as T;
}
