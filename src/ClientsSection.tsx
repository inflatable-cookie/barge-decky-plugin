import {
  ConfirmModal,
  DialogButton,
  Field,
  PanelSection,
  PanelSectionRow,
  showModal,
} from "@decky/ui";
import { useEffect, useState } from "react";
import { getClients, revokeClient } from "./api";
import type { PairedClientRecord } from "./types";

interface ClientsSectionProps {
  machineId: string;
  machineName: string;
}

function formatPairedAt(atMs: number): string {
  return new Date(atMs).toLocaleDateString([], {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

/**
 * A machine's paired clients. Only clients a machine has registered appear here;
 * revoking one takes effect on the machine immediately.
 */
export function ClientsSection({ machineId, machineName }: ClientsSectionProps) {
  const [clients, setClients] = useState<PairedClientRecord[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let active = true;
    setClients([]);
    setError(null);
    setNotice(null);
    void getClients(machineId)
      .then((res) => {
        if (!active) return;
        setClients(res.clients);
        if (!res.ok) setError(res.error ?? "Could not list clients");
      })
      .catch((err) => {
        if (!active) return;
        setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      active = false;
    };
  }, [machineId]);

  const revoke = (client: PairedClientRecord) => {
    showModal(
      <ConfirmModal
        strTitle={`Revoke ${client.name}?`}
        strDescription={
          client.this_deck
            ? "This is the client this Steam Deck uses. Revoking it removes this Deck's access to the machine until it pairs again."
            : `${client.name} will lose access to ${machineName} until it pairs again.`
        }
        strOKButtonText="Revoke"
        strCancelButtonText="Cancel"
        bDestructiveWarning={true}
        onOK={() => {
          setBusy(true);
          setError(null);
          setNotice(null);
          void revokeClient(client.id, machineId)
            .then((res) => {
              if (!res.ok) {
                setError(res.error ?? "Could not revoke client");
                return;
              }
              setClients(res.clients);
              setNotice(`${client.name} revoked`);
            })
            .catch((err) => {
              setError(err instanceof Error ? err.message : String(err));
            })
            .finally(() => {
              setBusy(false);
            });
        }}
      />,
    );
  };

  return (
    <PanelSection title="Paired Clients">
      {error && (
        <PanelSectionRow>
          <Field label="Error" description={error} />
        </PanelSectionRow>
      )}
      {notice && !error && (
        <PanelSectionRow>
          <Field label="Clients" description={notice} />
        </PanelSectionRow>
      )}
      {clients.length === 0 && !error && (
        <PanelSectionRow>
          <Field label="No clients" description="No client is paired with this machine yet." />
        </PanelSectionRow>
      )}
      {clients.map((client) => (
        <PanelSectionRow key={client.id}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, width: "100%" }}>
            <div style={{ flex: 1, minWidth: 0 }}>
              <Field
                label={client.this_deck ? `${client.name} (this Deck)` : client.name}
                description={`Paired ${formatPairedAt(client.paired_at_unix_ms)}`}
                bottomSeparator="none"
              />
            </div>
            <DialogButton
              style={{ width: "auto", padding: "8px 14px" }}
              disabled={busy}
              onClick={() => revoke(client)}
            >
              Revoke
            </DialogButton>
          </div>
        </PanelSectionRow>
      ))}
    </PanelSection>
  );
}
