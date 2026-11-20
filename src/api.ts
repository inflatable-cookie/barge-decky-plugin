import {
  addEventListener,
  callable,
  removeEventListener,
} from "@decky/api";
import type {
  ActionResponse,
  MachineSnapshot,
  PowerActionKind,
  SettingsResponse,
} from "./types";

export const getState = callable<[], MachineSnapshot>("get_state");
export const refreshState = callable<[], MachineSnapshot>("refresh_state");
export const getSettings = callable<[], SettingsResponse>("get_settings");

export const saveSettings = callable<
  [
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

export const pairKey = callable<[lan_key: string], ActionResponse>("pair_key");
export const importKeyFile = callable<[file_path: string], ActionResponse>(
  "import_key_file",
);
export const unpair = callable<[], ActionResponse>("unpair");
export const wakeMachine = callable<[], ActionResponse>("wake");
export const sendPower = callable<[action: PowerActionKind], ActionResponse>(
  "power",
);
export const cancelPower = callable<[], ActionResponse>("cancel");

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
