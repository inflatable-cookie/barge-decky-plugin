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
  /** barged's stable machine ID, empty until a poll or pairing learns it. */
  daemon_machine_id: string;
  /** The client ID this Deck paired as, empty for a manually imported key. */
  client_id: string;
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

/** A `barged` heard on the LAN that this Deck has not added. */
export interface DiscoveredMachine {
  id: string;
  name: string;
  mode: string;
  protocol_version: string;
  address: string;
  port: number;
}

export type PairingPhase =
  | "waiting"
  | "approved"
  | "denied"
  | "expired"
  | "failed";

/** One pairing request this Deck opened; the six-digit code is shown, the key is not. */
export interface PairingSession {
  status: PairingPhase;
  code: string;
  name: string;
  address: string;
  port: number;
  daemon_machine_id: string;
  request_id: string;
  expires_at_unix_ms: number;
  machine_id: string;
  error: string;
}

/** A request waiting for approval on this PC's own barged. */
export interface PendingPairRequest {
  request_id: string;
  client_name: string;
  code: string;
  created_at_unix_ms: number;
  expires_at_unix_ms: number;
}

/** This PC's own barged, which approves other clients from the panel. */
export interface ThisPcInfo {
  available: boolean;
  port: number;
  pending: PendingPairRequest[];
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
  discovered: DiscoveredMachine[];
  discover_error: string | null;
  pairing: PairingSession | null;
  this_pc: ThisPcInfo;
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

/** One of a machine's paired clients; `this_deck` marks the Deck showing the list. */
export interface PairedClientRecord {
  id: string;
  name: string;
  paired_at_unix_ms: number;
  this_deck: boolean;
}

export interface DiscoveryResponse {
  ok: boolean;
  error?: string;
  state: MachineSnapshot;
}

export interface PairingResponse {
  ok: boolean;
  error?: string;
  pairing: PairingSession | null;
  state: MachineSnapshot;
}

export interface PairDecisionResponse {
  ok: boolean;
  error?: string;
  approved?: boolean;
  client_name?: string;
  state: MachineSnapshot;
}

export interface ClientsResponse {
  ok: boolean;
  error?: string;
  clients: PairedClientRecord[];
}

export interface RevokeResponse {
  ok: boolean;
  error?: string;
  revoked_is_this_deck?: boolean;
  clients: PairedClientRecord[];
}
