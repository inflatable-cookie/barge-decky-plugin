import {
  addEventListener,
  callable,
  removeEventListener,
} from "@decky/api";
import type {
  ActionResponse,
  ClientsResponse,
  DiscoveryResponse,
  ErrorLogResponse,
  MachineSnapshot,
  PairDecisionResponse,
  PairingResponse,
  PowerActionKind,
  RevokeResponse,
  SettingsResponse,
} from "./types";

export const getState = callable<[], MachineSnapshot>("get_state");
export const refreshState = callable<[], MachineSnapshot>("refresh_state");
export const getSettings = callable<[], SettingsResponse>("get_settings");
export const getErrorLog = callable<[], ErrorLogResponse>("get_error_log");
export const selectMachine = callable<[machine_id: string], ActionResponse>(
  "select_machine",
);

/** Adds a machine when machine_id is "", otherwise edits that machine. */
export const saveSettings = callable<
  [
    machine_id: string,
    name: string,
    host: string,
    daemon_port: number,
    mac: string,
    broadcast: string,
    wol_port: number,
    delay_s: number,
    lan_key: string,
    key_file_path: string,
  ],
  ActionResponse
>("save_settings");

export const removeMachine = callable<[machine_id: string], ActionResponse>(
  "remove_machine",
);
export const importKeyFile = callable<
  [file_path: string, machine_id: string],
  ActionResponse
>("import_key_file");
export const unpair = callable<[machine_id: string], ActionResponse>("unpair");
export const wakeMachine = callable<[machine_id: string], ActionResponse>("wake");
export const sendPower = callable<
  [action: PowerActionKind, machine_id: string],
  ActionResponse
>("power");
export const cancelPower = callable<[machine_id: string], ActionResponse>("cancel");

/** Browse the LAN for barged and return the machines found so far. */
export const discoverMachines = callable<[], DiscoveryResponse>("discover_machines");

/** Add a discovered machine and open a pairing request with it. */
export const startPairing = callable<
  [discovered_id: string, client_name: string],
  PairingResponse
>("start_pairing");

/** Poll the waiting pairing request; the machine is saved once approved. */
export const pairingStatus = callable<[], PairingResponse>("pairing_status");
export const cancelPairing = callable<[], PairingResponse>("cancel_pairing");

/** Approve or deny a request on this PC's own barged. */
export const approvePair = callable<[request_id: string], PairDecisionResponse>(
  "approve_pair",
);
export const denyPair = callable<[request_id: string], PairDecisionResponse>("deny_pair");

export const getClients = callable<[machine_id: string], ClientsResponse>("get_clients");
export const revokeClient = callable<
  [client_id: string, machine_id: string],
  RevokeResponse
>("revoke_client");

export function subscribeState(
  onUpdate: (snapshot: MachineSnapshot) => void,
): () => void {
  const listener = addEventListener<[snapshot: MachineSnapshot]>(
    "barge_state",
    onUpdate,
  );
  return () => {
    removeEventListener("barge_state", listener);
  };
}
