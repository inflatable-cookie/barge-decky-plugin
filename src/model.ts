import type { MachineSnapshot, Offer } from "./types";

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

export function formatStateLabel(snapshot: MachineSnapshot, nowMs: number): string {
  switch (snapshot.phase) {
    case "offline": {
      if (snapshot.last_seen_unix_ms === null) return "Offline";
      if (nowMs - snapshot.last_seen_unix_ms < 5_000) return "Offline";
      return `Offline for ${offlineFor(snapshot.last_seen_unix_ms, nowMs)}`;
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
      return snapshot.action === "shutdown" ? "Shutting down…" : "Going to sleep…";
    case "pending": {
      const verb =
        snapshot.action === "restart"
          ? "Restarting"
          : snapshot.action === "shutdown"
            ? "Shutting down"
            : "Sleeping";
      const seconds =
        snapshot.deadline_unix_ms === null
          ? 0
          : countdownSeconds(snapshot.deadline_unix_ms, nowMs);
      return `${verb} in ${seconds} s`;
    }
  }
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
