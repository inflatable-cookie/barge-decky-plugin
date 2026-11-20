import { openFilePicker, type FileSelectionType } from "@decky/api";
import {
  ButtonItem,
  ConfirmModal,
  Field,
  PanelSection,
  PanelSectionRow,
  TextField,
  showModal,
} from "@decky/ui";
import { useEffect, useState } from "react";
import { getSettings, importKeyFile, saveSettings, unpair } from "./api";
import type { MachineSnapshot } from "./types";

interface SettingsViewProps {
  snapshot: MachineSnapshot;
  onUpdated: (next: MachineSnapshot) => void;
  onBack: () => void;
}

export function SettingsView({ snapshot, onUpdated, onBack }: SettingsViewProps) {
  const [name, setName] = useState(snapshot.machine.name || "bazzite");
  const [host, setHost] = useState(snapshot.machine.host);
  const [daemonPort, setDaemonPort] = useState(String(snapshot.machine.daemon_port || 9777));
  const [mac, setMac] = useState(snapshot.machine.mac);
  const [broadcast, setBroadcast] = useState(snapshot.machine.broadcast || "255.255.255.255");
  const [wolPort, setWolPort] = useState(String(snapshot.machine.wol_port || 9));
  const [delayS, setDelayS] = useState(String(snapshot.machine.delay_s ?? 15));
  const [lanKey, setLanKey] = useState("");
  const [paired, setPaired] = useState(snapshot.paired);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    void getSettings().then((res) => {
      if (!active) return;
      setPaired(res.paired);
      if (res.machine.name) setName(res.machine.name);
      if (res.machine.host) setHost(res.machine.host);
      setDaemonPort(String(res.machine.daemon_port || 9777));
      if (res.machine.mac) setMac(res.machine.mac);
      setBroadcast(res.machine.broadcast || "255.255.255.255");
      setWolPort(String(res.machine.wol_port || 9));
      setDelayS(String(res.machine.delay_s ?? 15));
      if (!res.ok && res.error) {
        setError(res.error);
      }
    });
    return () => {
      active = false;
    };
  }, []);

  const parseIntField = (raw: string, label: string): number | null => {
    const trimmed = raw.trim();
    if (!/^\d+$/.test(trimmed)) {
      setError(`${label} must be a whole number`);
      return null;
    }
    return Number.parseInt(trimmed, 10);
  };

  const handleSave = async () => {
    setError(null);
    setNotice(null);

    const parsedDaemonPort = parseIntField(daemonPort, "Daemon port");
    if (parsedDaemonPort === null) return;
    const parsedWolPort = parseIntField(wolPort, "Wake-on-LAN port");
    if (parsedWolPort === null) return;
    const parsedDelayS = parseIntField(delayS, "Countdown delay");
    if (parsedDelayS === null) return;

    setBusy(true);
    try {
      const res = await saveSettings(
        name,
        host,
        parsedDaemonPort,
        mac,
        broadcast,
        parsedWolPort,
        parsedDelayS,
        lanKey,
        "",
      );
      onUpdated(res.state);
      setPaired(res.state.paired);
      if (!res.ok) {
        setError(res.error ?? "Failed to save settings");
        return;
      }
      setLanKey("");
      setNotice("Settings saved");
      onBack();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const handleImportKeyFile = async () => {
    setError(null);
    setNotice(null);
    try {
      const picked = await openFilePicker(
        0 as FileSelectionType,
        "/home/deck",
        true,
        false,
        undefined,
        undefined,
        true,
        true,
      );
      const selectedPath = picked.realpath || picked.path;
      if (!selectedPath) return;
      setBusy(true);
      const res = await importKeyFile(selectedPath);
      onUpdated(res.state);
      setPaired(res.state.paired);
      if (!res.ok) {
        setError(res.error ?? "Could not import key file");
        return;
      }
      setNotice("LAN key imported and stored owner-only");
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      if (!message.toLowerCase().includes("cancel")) {
        setError(message);
      }
    } finally {
      setBusy(false);
    }
  };

  const handleUnpair = () => {
    showModal(
      <ConfirmModal
        strTitle="Remove pairing key?"
        strDescription="This clears the stored LAN key on this Steam Deck. You will need to paste or import a key from barged pair to query state or send power actions."
        strOKButtonText="Unpair"
        strCancelButtonText="Cancel"
        bDestructiveWarning={true}
        onOK={async () => {
          setBusy(true);
          setError(null);
          setNotice(null);
          try {
            const res = await unpair();
            onUpdated(res.state);
            setPaired(res.state.paired);
            if (!res.ok) {
              setError(res.error ?? "Could not remove pairing key");
            } else {
              setNotice("Pairing key removed");
            }
          } catch (err) {
            setError(err instanceof Error ? err.message : String(err));
          } finally {
            setBusy(false);
          }
        }}
      />,
    );
  };

  return (
    <>
      <PanelSection title="Machine">
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={busy} onClick={onBack}>
            Back
          </ButtonItem>
        </PanelSectionRow>
        {error && (
          <PanelSectionRow>
            <Field label="Error" description={error} />
          </PanelSectionRow>
        )}
        {notice && !error && (
          <PanelSectionRow>
            <Field label="Saved" description={notice} />
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <TextField
            label="Machine Name"
            value={name}
            placeholder="bazzite"
            disabled={busy}
            onChange={(e) => setName(e.target.value)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <TextField
            label="Host / LAN Address"
            value={host}
            placeholder="bazzite.local or 192.168.1.50"
            disabled={busy}
            onChange={(e) => setHost(e.target.value)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <TextField
            label="Daemon Port"
            value={daemonPort}
            mustBeNumeric={true}
            disabled={busy}
            onChange={(e) => setDaemonPort(e.target.value)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <TextField
            label="MAC Address"
            value={mac}
            placeholder="00:11:22:33:44:ff"
            disabled={busy}
            onChange={(e) => setMac(e.target.value)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <TextField
            label="Broadcast Address"
            value={broadcast}
            placeholder="255.255.255.255"
            disabled={busy}
            onChange={(e) => setBroadcast(e.target.value)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <TextField
            label="Wake-on-LAN Port"
            value={wolPort}
            mustBeNumeric={true}
            disabled={busy}
            onChange={(e) => setWolPort(e.target.value)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <TextField
            label="Countdown Delay (seconds)"
            value={delayS}
            mustBeNumeric={true}
            disabled={busy}
            onChange={(e) => setDelayS(e.target.value)}
          />
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="LAN Pairing">
        <PanelSectionRow>
          <Field
            label="Status"
            description={
              paired
                ? "Paired (key stored owner-only in Decky settings)"
                : "Not paired — run barged pair on the PC and paste or import the key"
            }
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <TextField
            label={paired ? "Replace LAN Key (optional)" : "LAN Key (64 hex chars)"}
            bIsPassword={true}
            value={lanKey}
            placeholder="Paste 64-character hex key"
            disabled={busy}
            onChange={(e) => setLanKey(e.target.value)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={busy} onClick={handleImportKeyFile}>
            Import Key File…
          </ButtonItem>
        </PanelSectionRow>
        {paired && (
          <PanelSectionRow>
            <ButtonItem layout="below" disabled={busy} onClick={handleUnpair}>
              Unpair…
            </ButtonItem>
          </PanelSectionRow>
        )}
      </PanelSection>

      <PanelSection>
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={busy} onClick={handleSave}>
            {busy ? "Saving…" : "Save Settings"}
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
}
