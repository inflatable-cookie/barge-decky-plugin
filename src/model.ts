import type {
  MachineSnapshot,
  MachineSummary,
  Offer,
  PairingSession,
  Phase,
  PowerActionKind,
} from "./types";

/** The fields that decide a state label. Both a snapshot and a dropdown summary carry them. */
export interface StatusFields {
  phase: Phase;
  action: PowerActionKind | null;
  deadline_unix_ms: number | null;
  last_seen_unix_ms: number | null;
}

export function countdownSeconds(deadlineMs: number, nowMs: number): number {
  return Math.max(0, Math.ceil((deadlineMs - nowMs) / 1000));
}

export function offlineFor(atMs: number, nowMs: number): string {
  const seconds = Math.floor(Math.max(0, nowMs - atMs) / 1000);
  if (seconds < 60) return `${seconds} s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours} h`;
  return `${Math.floor(hours / 24)} d`;
}

export function formatStateLabel(status: StatusFields, nowMs: number): string {
  switch (status.phase) {
    case "offline": {
      if (status.last_seen_unix_ms === null) return "Offline";
      if (nowMs - status.last_seen_unix_ms < 5_000) return "Offline";
      return `Offline for ${offlineFor(status.last_seen_unix_ms, nowMs)}`;
    }
    case "checking":
      return "Checking…";
    case "waking":
      return "Waking…";
    case "awake":
      return "Awake";
    case "unknown":
      return "Unknown";
    case "did_not_wake":
      return "Didn't wake";
    case "still_on":
      return "Still on";
    case "restarting":
      return "Restarting…";
    case "going_down":
      return status.action === "shutdown" ? "Shutting down…" : "Going to sleep…";
    case "pending": {
      const verb =
        status.action === "restart"
          ? "Restarting"
          : status.action === "shutdown"
            ? "Shutting down"
            : "Sleeping";
      const seconds =
        status.deadline_unix_ms === null
          ? 0
          : countdownSeconds(status.deadline_unix_ms, nowMs);
      return `${verb} in ${seconds} s`;
    }
  }
}

/** Dropdown text for one machine: its name, then its state (or that it is not paired). */
export function machineOptionLabel(machine: MachineSummary, nowMs: number): string {
  const state = machine.paired ? formatStateLabel(machine, nowMs) : "Not paired";
  return `${machine.name} · ${state}`;
}

export function formatLogTime(atMs: number): string {
  return new Date(atMs).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function offerLabel(offer: Offer): string {
  switch (offer) {
    case "wake":
      return "Wake";
    case "sleep":
      return "Sleep";
    case "restart":
      return "Restart…";
    case "shutdown":
      return "Shut down…";
    case "cancel":
      return "Cancel";
  }
}

export function needsConfirmation(offer: Offer): boolean {
  return offer === "restart" || offer === "shutdown";
}

export function countdownPercent(snapshot: MachineSnapshot, nowMs: number): number {
  if (snapshot.phase !== "pending" || snapshot.deadline_unix_ms === null) {
    return 0;
  }
  const totalSeconds = Math.max(1, snapshot.machine.delay_s || 15);
  const remaining = countdownSeconds(snapshot.deadline_unix_ms, nowMs);
  const ratio = Math.min(1, Math.max(0, remaining / totalSeconds));
  return Math.round(ratio * 100);
}

/** The six-digit pairing code, split for easy reading on a TV-sized screen. */
export function formatPairingCode(code: string): string {
  return /^\d{6}$/.test(code) ? `${code.slice(0, 3)} ${code.slice(3)}` : code;
}

/** What Settings says about a pairing request that is no longer waiting. */
export function pairingOutcome(session: PairingSession): string {
  switch (session.status) {
    case "approved":
      return `Paired with ${session.name}`;
    case "denied":
      return `${session.name} denied the pairing request`;
    case "expired":
      return "The pairing request expired — search again and add the machine";
    case "failed":
      return session.error || "Pairing failed";
    default:
      return "";
  }
}

/** Remaining time for a pending approval on this PC. */
export function expiresInSeconds(expiresAtMs: number, nowMs: number): number {
  return Math.max(0, Math.ceil((expiresAtMs - nowMs) / 1000));
}
