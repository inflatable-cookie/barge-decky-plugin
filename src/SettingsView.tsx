import { openFilePicker, type FileSelectionType } from "@decky/api";
import {
  ButtonItem,
  ConfirmModal,
  Dropdown,
  Field,
  PanelSection,
  PanelSectionRow,
  TextField,
  showModal,
} from "@decky/ui";
import { useEffect, useState } from "react";
import {
  cancelPairing,
  discoverMachines,
  getSettings,
  importKeyFile,
  removeMachine,
  saveSettings,
  startPairing,
  unpair,
} from "./api";
import { ClientsSection } from "./ClientsSection";
import { DiscoverySection } from "./DiscoverySection";
import type { MachineSettingsRecord, MachineSnapshot } from "./types";

/** New machines are edited with an empty id; this non-empty value is the dropdown's "Add machine…" option. */
const ADD_NEW = "";
const ADD_OPTION = "__add_machine__";

interface SettingsViewProps {
  snapshot: MachineSnapshot;
  onUpdated: (next: MachineSnapshot) => void;
  onBack: () => void;
}

interface MachineForm {
  name: string;
  host: string;
  daemonPort: string;
  mac: string;
  broadcast: string;
  wolPort: string;
  delayS: string;
}

const BLANK_FORM: MachineForm = {
  name: "",
  host: "",
  daemonPort: "9777",
  mac: "",
  broadcast: "255.255.255.255",
  wolPort: "9",
  delayS: "15",
};

function formFrom(record: MachineSettingsRecord | null): MachineForm {
  if (!record) return BLANK_FORM;
  return {
    name: record.name,
    host: record.host,
    daemonPort: String(record.daemon_port),
    mac: record.mac,
    broadcast: record.broadcast,
    wolPort: String(record.wol_port),
    delayS: String(record.delay_s),
  };
}

export function SettingsView({ snapshot, onUpdated, onBack }: SettingsViewProps) {
  const [machines, setMachines] = useState<MachineSettingsRecord[]>([]);
  const [editingId, setEditingId] = useState<string>(ADD_NEW);
  const [form, setForm] = useState<MachineForm>(BLANK_FORM);
  const [lanKey, setLanKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const editing = machines.find((m) => m.id === editingId) ?? null;
  const paired = editing?.paired ?? false;

  /** Replace the list and open one machine in the form (or the add form when none is given). */
  const focusMachine = (records: MachineSettingsRecord[], wantedId: string) => {
    const target = records.find((m) => m.id === wantedId) ?? records[0] ?? null;
    setMachines(records);
    setEditingId(target?.id ?? ADD_NEW);
    setForm(formFrom(target));
    setLanKey("");
  };

  const chooseMachine = (id: string) => {
    setError(null);
    setNotice(null);
    setEditingId(id);
    setForm(formFrom(machines.find((m) => m.id === id) ?? null));
    setLanKey("");
  };

  const reloadMachines = async () => {
    const res = await getSettings();
    if (res.ok) setMachines(res.machines);
  };

  useEffect(() => {
    let active = true;
    void getSettings().then((res) => {
      if (!active) return;
      if (!res.ok && res.error) setError(res.error);
      focusMachine(res.machines, snapshot.selected_id);
    });
    return () => {
      active = false;
    };
  }, []);

  // Search once when Settings opens, so found machines appear without a tap.
  useEffect(() => {
    let active = true;
    void discoverMachines()
      .then((res) => {
        if (!active) return;
        onUpdated(res.state);
        if (!res.ok && res.error) setError(res.error);
      })
      .catch(() => {
        /* discovery is best effort; the Search again button retries */
      });
    return () => {
      active = false;
    };
  }, []);

  // Follow a pairing that finished while Settings was open.
  const approvedMachineId =
    snapshot.pairing?.status === "approved" ? snapshot.pairing.machine_id : "";
  useEffect(() => {
    if (!approvedMachineId) return;
    let active = true;
    void getSettings().then((res) => {
      if (!active || !res.ok) return;
      setMachines(res.machines);
      const target = res.machines.find((m) => m.id === approvedMachineId);
      if (target) {
        setEditingId(target.id);
        setForm(formFrom(target));
        setLanKey("");
      }
    });
    return () => {
      active = false;
    };
  }, [approvedMachineId]);

  const parseIntField = (raw: string, label: string): number | null => {
    const trimmed = raw.trim();
    if (!/^\d+$/.test(trimmed)) {
      setError(`${label} must be a whole number`);
      return null;
    }
    return Number.parseInt(trimmed, 10);
  };

  /** Runs one settings action with the busy flag, clearing old messages and showing any failure. */
  const runBusy = async (
    work: () => Promise<void>,
    options: { ignoreCancel?: boolean } = {},
  ) => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await work();
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      const cancelled = message.toLowerCase().includes("cancel");
      if (!(options.ignoreCancel && cancelled)) setError(message);
    } finally {
      setBusy(false);
    }
  };

  const handleSearch = () => {
    void runBusy(async () => {
      const res = await discoverMachines();
      onUpdated(res.state);
      if (!res.ok) setError(res.error ?? "Could not search the network");
    });
  };

  const handleAdd = (discoveredId: string) => {
    void runBusy(async () => {
      const res = await startPairing(discoveredId, "");
      onUpdated(res.state);
      if (!res.ok) setError(res.error ?? "Could not start pairing");
    });
  };

  const handleCancelPairing = () => {
    void runBusy(async () => {
      const res = await cancelPairing();
      onUpdated(res.state);
    });
  };

  const handleSave = () => {
    const daemonPort = parseIntField(form.daemonPort, "Daemon port");
    if (daemonPort === null) return;
    const wolPort = parseIntField(form.wolPort, "Wake-on-LAN port");
    if (wolPort === null) return;
    const delayS = parseIntField(form.delayS, "Countdown delay");
    if (delayS === null) return;

    void runBusy(async () => {
      const res = await saveSettings(
        editingId,
        form.name,
        form.host,
        daemonPort,
        form.mac,
        form.broadcast,
        wolPort,
        delayS,
        lanKey,
        "",
      );
      onUpdated(res.state);
      if (!res.ok) {
        setError(res.error ?? "Failed to save settings");
        return;
      }
      onBack();
    });
  };

  const handleImportKeyFile = () => {
    if (!editing) return;
    const targetId = editing.id;
    void runBusy(
      async () => {
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
        const res = await importKeyFile(selectedPath, targetId);
        onUpdated(res.state);
        if (!res.ok) {
          setError(res.error ?? "Could not import key file");
          return;
        }
        await reloadMachines();
        setNotice("LAN key imported and stored owner-only");
      },
      { ignoreCancel: true },
    );
  };

  const handleUnpair = () => {
    if (!editing) return;
    const target = editing;
    showModal(
      <ConfirmModal
        strTitle={`Remove the pairing key for ${target.name}?`}
        strDescription="This clears the stored LAN key for this machine on this Steam Deck. Pair it again, or paste or import a key from barged pair, to query state or send power actions."
        strOKButtonText="Unpair"
        strCancelButtonText="Cancel"
        bDestructiveWarning={true}
        onOK={() =>
          runBusy(async () => {
            const res = await unpair(target.id);
            onUpdated(res.state);
            if (!res.ok) {
              setError(res.error ?? "Could not remove pairing key");
              return;
            }
            await reloadMachines();
            setNotice("Pairing key removed");
          })
        }
      />,
    );
  };

  const handleRemove = () => {
    if (!editing) return;
    const target = editing;
    showModal(
      <ConfirmModal
        strTitle={`Remove ${target.name}?`}
        strDescription="This forgets the machine and deletes its LAN key from this Steam Deck. Pair it again to add it back."
        strOKButtonText="Remove"
        strCancelButtonText="Cancel"
        bDestructiveWarning={true}
        onOK={() =>
          runBusy(async () => {
            const res = await removeMachine(target.id);
            onUpdated(res.state);
            if (!res.ok || !res.settings) {
              setError(res.error ?? "Could not remove machine");
              return;
            }
            focusMachine(res.settings.machines, res.state.selected_id);
            setNotice(`${target.name} removed`);
          })
        }
      />,
    );
  };

  const machineOptions = [
    ...machines.map((m) => ({
      data: m.id,
      label: m.paired ? m.name : `${m.name} · not paired`,
    })),
    { data: ADD_OPTION, label: "Add machine…" },
  ];

  const pairingStatus = !editing
    ? "Paste the key below to pair this machine when you save, or import a key file after saving."
    : paired
      ? "Paired (key stored owner-only in Decky settings)"
      : "Not paired — add the machine above, or paste or import a key";

  return (
    <>
      <PanelSection title="Machines">
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            bottomSeparator="none"
            disabled={busy}
            onClick={onBack}
          >
            Back
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <Dropdown
            rgOptions={machineOptions}
            selectedOption={editingId === ADD_NEW ? ADD_OPTION : editingId}
            disabled={busy}
            onChange={(option) => {
              const picked = String(option.data);
              chooseMachine(picked === ADD_OPTION ? ADD_NEW : picked);
            }}
          />
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
      </PanelSection>

      <DiscoverySection
        snapshot={snapshot}
        busy={busy}
        onSearch={handleSearch}
        onAdd={handleAdd}
        onCancelPairing={handleCancelPairing}
      />

      <PanelSection title={editing ? "Machine" : "Add Manually"}>
        <PanelSectionRow>
          <Field
            label={editing ? "Details" : "Manual entry"}
            description={
              editing
                ? "Change this machine's address, MAC or action timing."
                : "Use this when the machine is not found on the network."
            }
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <TextField
            label="Machine Name"
            value={form.name}
            placeholder="bazzite"
            disabled={busy}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <TextField
            label="Host / LAN Address"
            value={form.host}
            placeholder="192.168.1.50"
            disabled={busy}
            onChange={(e) => setForm({ ...form, host: e.target.value })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <TextField
            label="Daemon Port"
            value={form.daemonPort}
            mustBeNumeric={true}
            disabled={busy}
            onChange={(e) => setForm({ ...form, daemonPort: e.target.value })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <TextField
            label="MAC Address"
            value={form.mac}
            placeholder="00:11:22:33:44:ff"
            disabled={busy}
            onChange={(e) => setForm({ ...form, mac: e.target.value })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <TextField
            label="Broadcast Address"
            value={form.broadcast}
            placeholder="255.255.255.255"
            disabled={busy}
            onChange={(e) => setForm({ ...form, broadcast: e.target.value })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <TextField
            label="Wake-on-LAN Port"
            value={form.wolPort}
            mustBeNumeric={true}
            disabled={busy}
            onChange={(e) => setForm({ ...form, wolPort: e.target.value })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <TextField
            label="Countdown Delay (seconds)"
            value={form.delayS}
            mustBeNumeric={true}
            disabled={busy}
            onChange={(e) => setForm({ ...form, delayS: e.target.value })}
          />
        </PanelSectionRow>
        {editing && (
          <PanelSectionRow>
            <ButtonItem layout="below" disabled={busy} onClick={handleRemove}>
              Remove Machine…
            </ButtonItem>
          </PanelSectionRow>
        )}
      </PanelSection>

      {editing && paired && (
        <ClientsSection machineId={editing.id} machineName={editing.name} />
      )}

      <PanelSection title="Manual Pairing Key (Fallback)">
        <PanelSectionRow>
          <Field label="Status" description={pairingStatus} bottomSeparator="none" />
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
          <ButtonItem
            layout="below"
            bottomSeparator="none"
            disabled={busy || !editing}
            onClick={handleImportKeyFile}
          >
            Import Key File…
          </ButtonItem>
        </PanelSectionRow>
        {paired && (
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              bottomSeparator="none"
              disabled={busy}
              onClick={handleUnpair}
            >
              Unpair…
            </ButtonItem>
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            bottomSeparator="none"
            disabled={busy}
            onClick={handleSave}
          >
            {busy ? "Saving…" : editing ? "Save Machine" : "Add Machine"}
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
}
