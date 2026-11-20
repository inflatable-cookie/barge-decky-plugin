export type PowerActionKind = "sleep" | "restart" | "shutdown";

export type Phase =
  | "offline"
  | "checking"
  | "waking"
  | "awake"
  | "pending"
  | "going_down"
  | "restarting"
  | "did_not_wake"
  | "still_on"
  | "unknown";

export type Offer = "wake" | "sleep" | "restart" | "shutdown" | "cancel";

export interface MachineSettings {
  name: string;
  host: string;
  daemon_port: number;
  mac: string;
  broadcast: string;
  wol_port: number;
  delay_s: number;
}

export interface MachineSnapshot {
  configured: boolean;
  paired: boolean;
  machine: MachineSettings;
  reachable: boolean;
  error: string | null;
  seen_at_unix_ms: number | null;
  server_now_unix_ms: number;
  phase: Phase;
  action: PowerActionKind | null;
  deadline_unix_ms: number | null;
  last_seen_unix_ms: number | null;
  offers: Offer[];
  waiting: boolean;
  label: string;
  poll_interval_ms: number;
}

export interface SettingsResponse {
  ok: boolean;
  error?: string;
  paired: boolean;
  machine: MachineSettings;
}

export interface ActionResponse {
  ok: boolean;
  error?: string;
  cancelled?: boolean;
  state: MachineSnapshot;
  settings?: SettingsResponse;
}
