import {
  ButtonItem,
  ConfirmModal,
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
  FaMoon,
  FaPowerOff,
  FaSync,
} from "react-icons/fa";
import { cancelPower, refreshState, sendPower, wakeMachine } from "./api";
import {
  countdownPercent,
  formatStateLabel,
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
  const [actionError, setActionError] = useState<string | null>(null);

  const runAction = async (
    label: string,
    fn: () => Promise<ActionResponse>,
  ) => {
    setBusy(true);
    setActionError(null);
    try {
      const res = await fn();
      onUpdated(res.state);
      if (!res.ok) {
        setActionError(
          label === "Cancel"
            ? `Cancel could not be confirmed: ${res.error ?? "request failed"}`
            : `${label} failed: ${res.error ?? "request failed"}`,
        );
      }
    } catch (err) {
      const detail = err instanceof Error ? err.message : String(err);
      setActionError(
        label === "Cancel"
          ? `Cancel could not be confirmed: ${detail}`
          : `${label} failed: ${detail}`,
      );
    } finally {
      setBusy(false);
    }
  };

  const handleOffer = (offer: Offer) => {
    if (offer === "wake") {
      void runAction("Wake", wakeMachine);
      return;
    }
    if (offer === "cancel") {
      void runAction("Cancel", cancelPower);
      return;
    }
    if (offer === "sleep") {
      void runAction("Sleep", () => sendPower("sleep"));
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
              sendPower(offer),
            );
          }}
        />,
      );
    }
  };

  const handleRefresh = async () => {
    setBusy(true);
    setActionError(null);
    try {
      const next = await refreshState();
      onUpdated(next);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  if (!snapshot.configured) {
    return (
      <PanelSection title="Machine">
        <PanelSectionRow>
          <Field
            label="No machine configured"
            description="Set your PC's hostname, MAC address, and LAN pairing key to get started."
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={onOpenSettings}>
            Configure Machine
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    );
  }

  const label = formatStateLabel(snapshot, nowMs);
  const subtitle = `LAN · ${snapshot.machine.host}:${snapshot.machine.daemon_port}`;
  const unreachablePending =
    snapshot.phase === "pending" && !snapshot.reachable;
  const visibleError = actionError ?? snapshot.error;

  return (
    <>
      <PanelSection title={snapshot.machine.name}>
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

        {unreachablePending && (
          <PanelSectionRow>
            <Field
              label="Connection lost"
              description="Can't reach the machine. A cancel can't be confirmed until it answers again."
            />
          </PanelSectionRow>
        )}

        {visibleError && (
          <PanelSectionRow>
            <Field label="Error" description={visibleError} />
          </PanelSectionRow>
        )}

        {snapshot.offers.map((offer) => (
          <PanelSectionRow key={offer}>
            <ButtonItem
              layout="below"
              disabled={busy}
              onClick={() => handleOffer(offer)}
            >
              {offerLabel(offer)}
            </ButtonItem>
          </PanelSectionRow>
        ))}
      </PanelSection>

      <PanelSection>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={busy || snapshot.waiting}
            onClick={handleRefresh}
          >
            <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
              <FaSync /> Refresh
            </span>
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={busy} onClick={onOpenSettings}>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
              <FaCog /> Settings
            </span>
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
}
