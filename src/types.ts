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
  id: string;
  name: string;
  host: string;
  daemon_port: number;
  mac: string;
  broadcast: string;
  wol_port: number;
  delay_s: number;
}

/** A machine as the Settings view edits it; `paired` says whether its LAN key is stored. */
export interface MachineSettingsRecord extends MachineSettings {
  paired: boolean;
}

/** One row of the machine dropdown: enough state to label it without another round trip. */
export interface MachineSummary {
  id: string;
  name: string;
  host: string;
  configured: boolean;
  paired: boolean;
  reachable: boolean;
  phase: Phase;
  action: PowerActionKind | null;
  deadline_unix_ms: number | null;
  last_seen_unix_ms: number | null;
  waiting: boolean;
}

/** A short line for the panel. Only errors the user must act on reach it. */
export interface Attention {
  message: string;
}

export interface MachineSnapshot {
  configured: boolean;
  paired: boolean;
  machine: MachineSettings;
  selected_id: string;
  machines: MachineSummary[];
  attention: Attention | null;
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
  selected_id: string;
  machines: MachineSettingsRecord[];
}

export interface ActionResponse {
  ok: boolean;
  error?: string;
  cancelled?: boolean;
  machine_id?: string;
  state: MachineSnapshot;
  settings?: SettingsResponse;
}

export interface ErrorLogEntry {
  at_unix_ms: number;
  machine_id: string;
  machine_name: string;
  kind: string;
  message: string;
}

export interface ErrorLogResponse {
  ok: boolean;
  entries: ErrorLogEntry[];
}
