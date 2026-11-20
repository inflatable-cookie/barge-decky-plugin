import {
  ButtonItem,
  ConfirmModal,
  DialogButton,
  Dropdown,
  Field,
  PanelSection,
  PanelSectionRow,
  ProgressBar,
  showModal,
} from "@decky/ui";
import { useState } from "react";
import {
  FaCheckCircle,
  FaCog,
  FaExclamationTriangle,
  FaList,
  FaMoon,
  FaPowerOff,
  FaSync,
} from "react-icons/fa";
import {
  cancelPower,
  getErrorLog,
  refreshState,
  selectMachine,
  sendPower,
  wakeMachine,
} from "./api";
import { ErrorLogModal } from "./ErrorLogModal";
import {
  countdownPercent,
  formatStateLabel,
  machineOptionLabel,
  needsConfirmation,
  offerLabel,
} from "./model";
import type { ActionResponse, MachineSnapshot, Offer } from "./types";

interface MachineViewProps {
  snapshot: MachineSnapshot;
  nowMs: number;
  onUpdated: (next: MachineSnapshot) => void;
  onOpenSettings: () => void;
}

const VISUALLY_HIDDEN = {
  position: "absolute",
  width: 1,
  height: 1,
  overflow: "hidden",
  clip: "rect(0 0 0 0)",
  whiteSpace: "nowrap",
} as const;

const ICON_BUTTON_STYLE = {
  flex: 1,
  minWidth: 0,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  padding: "10px 0",
} as const;

function statusIcon(snapshot: MachineSnapshot) {
  switch (snapshot.phase) {
    case "awake":
      return <FaCheckCircle color="#4ade80" />;
    case "did_not_wake":
    case "still_on":
      return <FaExclamationTriangle color="#facc15" />;
    case "pending":
    case "going_down":
    case "restarting":
    case "waking":
      return <FaMoon color="#60a5fa" />;
    default:
      return <FaPowerOff color="#9ca3af" />;
  }
}

export function MachineView({
  snapshot,
  nowMs,
  onUpdated,
  onOpenSettings,
}: MachineViewProps) {
  const [busy, setBusy] = useState(false);
  // Only thrown RPC failures land here; refused actions arrive as snapshot.attention.
  const [rpcError, setRpcError] = useState<string | null>(null);

  const machineId = snapshot.selected_id;

  const runAction = async (
    label: string,
    fn: () => Promise<ActionResponse>,
  ) => {
    setBusy(true);
    setRpcError(null);
    try {
      const res = await fn();
      onUpdated(res.state);
    } catch (err) {
      const detail = err instanceof Error ? err.message : String(err);
      setRpcError(`${label} failed: ${detail}`);
    } finally {
      setBusy(false);
    }
  };

  const handleOffer = (offer: Offer) => {
    if (offer === "wake") {
      void runAction("Wake", () => wakeMachine(machineId));
      return;
    }
    if (offer === "cancel") {
      void runAction("Cancel", () => cancelPower(machineId));
      return;
    }
    if (offer === "sleep") {
      void runAction("Sleep", () => sendPower("sleep", machineId));
      return;
    }
    if (needsConfirmation(offer)) {
      const machineName = snapshot.machine.name || snapshot.machine.host;
      const isRestart = offer === "restart";
      const title = isRestart
        ? `Restart ${machineName}?`
        : `Shut down ${machineName}?`;
      const delay = snapshot.machine.delay_s || 15;
      const description = `This interrupts work on ${machineName} after a ${delay} s countdown.`;
      showModal(
        <ConfirmModal
          strTitle={title}
          strDescription={description}
          strOKButtonText={isRestart ? "Restart" : "Shut down"}
          strCancelButtonText="Cancel"
          bDestructiveWarning={true}
          onOK={() => {
            void runAction(isRestart ? "Restart" : "Shut down", () =>
              sendPower(offer, machineId),
            );
          }}
        />,
      );
    }
  };

  const handleRefresh = async () => {
    setBusy(true);
    setRpcError(null);
    try {
      const next = await refreshState();
      onUpdated(next);
    } catch (err) {
      setRpcError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const handleSelect = async (nextId: string) => {
    if (nextId === machineId) return;
    setRpcError(null);
    try {
      const res = await selectMachine(nextId);
      onUpdated(res.state);
    } catch (err) {
      setRpcError(err instanceof Error ? err.message : String(err));
    }
  };

  const openErrorLog = async () => {
    try {
      const res = await getErrorLog();
      showModal(<ErrorLogModal entries={res.entries} />);
    } catch (err) {
      setRpcError(err instanceof Error ? err.message : String(err));
    }
  };

  const attention = rpcError
    ? { message: rpcError }
    : snapshot.attention;

  const attentionRows = attention ? (
    <>
      <PanelSectionRow>
        <Field
          label="Needs attention"
          description={attention.message}
          bottomSeparator="none"
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" bottomSeparator="none" onClick={openErrorLog}>
          View error log
        </ButtonItem>
      </PanelSectionRow>
    </>
  ) : null;

  const bottomRow = (
    <PanelSection>
      <PanelSectionRow>
        <div style={{ display: "flex", gap: 8, width: "100%" }}>
          <DialogButton
            aria-label="Refresh"
            style={ICON_BUTTON_STYLE}
            disabled={busy || snapshot.waiting}
            onClick={handleRefresh}
          >
            <FaSync aria-hidden="true" />
            <span style={VISUALLY_HIDDEN}>Refresh</span>
          </DialogButton>
          <DialogButton
            aria-label="Settings"
            style={ICON_BUTTON_STYLE}
            disabled={busy}
            onClick={onOpenSettings}
          >
            <FaCog aria-hidden="true" />
            <span style={VISUALLY_HIDDEN}>Settings</span>
          </DialogButton>
          <DialogButton
            aria-label="Error log"
            style={ICON_BUTTON_STYLE}
            onClick={openErrorLog}
          >
            <FaList aria-hidden="true" />
            <span style={VISUALLY_HIDDEN}>Error log</span>
          </DialogButton>
        </div>
      </PanelSectionRow>
    </PanelSection>
  );

  if (snapshot.machines.length === 0) {
    return (
      <>
        {attentionRows}
        <PanelSection title="Machine">
          <PanelSectionRow>
            <Field
              label="No machine configured"
              description="Set your PC's hostname, MAC address, and LAN pairing key to get started."
            />
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem layout="below" bottomSeparator="none" onClick={onOpenSettings}>
              Configure Machine
            </ButtonItem>
          </PanelSectionRow>
        </PanelSection>
        {bottomRow}
      </>
    );
  }

  const label = formatStateLabel(snapshot, nowMs);
  const subtitle = `LAN · ${snapshot.machine.host}:${snapshot.machine.daemon_port}`;
  const machineOptions = snapshot.machines.map((machine) => ({
    data: machine.id,
    label: machineOptionLabel(machine, nowMs),
  }));

  return (
    <>
      <PanelSection>
        <PanelSectionRow>
          <Dropdown
            rgOptions={machineOptions}
            selectedOption={machineId}
            onChange={(option) => void handleSelect(String(option.data))}
          />
        </PanelSectionRow>

        <PanelSectionRow>
          <Field
            label={label}
            description={subtitle}
            icon={statusIcon(snapshot)}
          />
        </PanelSectionRow>

        {snapshot.phase === "pending" && (
          <PanelSectionRow>
            <div style={{ width: "100%", padding: "4px 0 8px" }}>
              <ProgressBar
                nProgress={countdownPercent(snapshot, nowMs)}
                nTransitionSec={1}
              />
            </div>
          </PanelSectionRow>
        )}

        {snapshot.waiting && snapshot.phase !== "pending" && (
          <PanelSectionRow>
            <div style={{ width: "100%", padding: "4px 0 8px" }}>
              <ProgressBar indeterminate={true} />
            </div>
          </PanelSectionRow>
        )}

        {attentionRows}

        {snapshot.offers.map((offer) => (
          <PanelSectionRow key={offer}>
            <ButtonItem
              layout="below"
              bottomSeparator="none"
              disabled={busy}
              onClick={() => handleOffer(offer)}
            >
              {offerLabel(offer)}
            </ButtonItem>
          </PanelSectionRow>
        ))}
      </PanelSection>

      {bottomRow}
    </>
  );
}
