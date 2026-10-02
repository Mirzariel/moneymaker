// Semua request lewat proxy server-side (/api/bot/*). Token tidak ada di browser.

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export async function api<T>(
  path: string,
  opts?: { method?: "GET" | "POST"; body?: unknown },
): Promise<T> {
  const method = opts?.method ?? "GET";
  let res: Response;
  try {
    res = await fetch(`/api/bot/${path}`, {
      method,
      cache: "no-store",
      headers: method === "POST" ? { "Content-Type": "application/json" } : undefined,
      body: method === "POST" ? JSON.stringify(opts?.body ?? {}) : undefined,
    });
  } catch {
    throw new ApiError(0, "Tidak bisa menghubungi dashboard server");
  }
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
    throw new ApiError(res.status, detail);
  }
  return data as T;
}
