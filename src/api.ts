import {
  addEventListener,
  callable,
  removeEventListener,
} from "@decky/api";
import type {
  ActionResponse,
  ErrorLogResponse,
  MachineSnapshot,
  PowerActionKind,
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
