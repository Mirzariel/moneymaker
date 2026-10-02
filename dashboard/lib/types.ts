export type BotStatus = "RUNNING" | "PAUSED";
export type Mode = "paper" | "live";

export interface Status {
  status: BotStatus;
  status_reason: string;
  status_changed_at: number | null;
  mode: Mode;
  quote: string;
  timeframe: string;
  last_cycle_at: number | null;
  last_error: string | null;
  open_positions: number;
  equity: number | null;
  quote_free: number | null;
  exposure: number | null;
  day_pnl: number | null;
  universe: string[];
  server_time: number;
}

export interface Position {
  id: number | string;
  symbol: string;
  status: string;
  amount: number | null;
  entry_price: number | null;
  cost: number | null;
  stop_price: number | null;
  take_profit: number | null;
  stop_order_id: string | null;
  opened_at: number | null;
  closed_at: number | null;
  exit_price: number | null;
  proceeds: number | null;
  pnl: number | null;
  fees: number | null;
  exit_reason: string | null;
  last_price: number | null;
  unrealized_pnl: number | null;
}

export interface EquityPoint {
  ts: number;
  equity: number;
  quote_free: number | null;
  exposure: number | null;
}

export interface Signal {
  id: number | string;
  ts: number;
  symbol: string;
  side: string;
  entry: number | null;
  stop: number | null;
  take_profit: number | null;
  expires_at: number | null;
  reason: string | null;
  decision: "approved" | "rejected" | string;
  decision_detail: unknown;
}

export interface RiskEvent {
  id: number | string;
  ts: number;
  symbol: string | null;
  rule: string;
  detail: unknown;
  action: string | null;
}

export interface AuditEntry {
  id: number | string;
  ts: number;
  actor: string;
  action: string;
  detail: unknown;
}

export interface PanicReport {
  cancelled: number;
  sold: string[];
  errors: string[];
  leftover: Record<string, number>;
}
