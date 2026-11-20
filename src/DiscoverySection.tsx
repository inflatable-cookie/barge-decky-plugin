import {
  ButtonItem,
  Field,
  PanelSection,
  PanelSectionRow,
  ProgressBar,
} from "@decky/ui";
import { formatPairingCode, pairingOutcome } from "./model";
import type { MachineSnapshot } from "./types";

interface DiscoverySectionProps {
  snapshot: MachineSnapshot;
  busy: boolean;
  onSearch: () => void;
  onAdd: (discoveredId: string) => void;
  onCancelPairing: () => void;
}

/**
 * The "Found on Your Network" section: browse results with Add, and the
 * six-digit code while a pairing request waits for approval elsewhere.
 */
export function DiscoverySection({
  snapshot,
  busy,
  onSearch,
  onAdd,
  onCancelPairing,
}: DiscoverySectionProps) {
  const pairing = snapshot.pairing;
  const waiting = pairing?.status === "waiting";
  const discovered = snapshot.discovered;

  return (
    <PanelSection title="Found on Your Network">
      {waiting && pairing && (
        <>
          <PanelSectionRow>
            <Field
              label={`Code ${formatPairingCode(pairing.code)}`}
              description={`Waiting for approval on ${pairing.name}. Approve the request there to finish pairing.`}
            />
          </PanelSectionRow>
          <PanelSectionRow>
            <div style={{ width: "100%", padding: "4px 0 8px" }}>
              <ProgressBar indeterminate={true} />
            </div>
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem layout="below" disabled={busy} onClick={onCancelPairing}>
              Cancel Pairing
            </ButtonItem>
          </PanelSectionRow>
        </>
      )}

      {pairing && !waiting && (
        <>
          <PanelSectionRow>
            <Field label="Pairing" description={pairingOutcome(pairing)} />
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem layout="below" disabled={busy} onClick={onCancelPairing}>
              Dismiss
            </ButtonItem>
          </PanelSectionRow>
        </>
      )}

      {!pairing && (
        <>
          {discovered.length === 0 && !snapshot.discover_error && (
            <PanelSectionRow>
              <Field
                label="No machines found yet"
                description="Start barged on the PC and keep both devices on the same network, then search again."
              />
            </PanelSectionRow>
          )}
          {snapshot.discover_error && (
            <PanelSectionRow>
              <Field label="Search failed" description={snapshot.discover_error} />
            </PanelSectionRow>
          )}
          {discovered.map((found) =>
            found.added ? (
              <PanelSectionRow key={found.id}>
                <Field
                  label={found.name}
                  description={`Added · ${found.address}:${found.port}`}
                />
              </PanelSectionRow>
            ) : (
              <PanelSectionRow key={found.id}>
                <ButtonItem
                  layout="below"
                  disabled={busy}
                  onClick={() => onAdd(found.id)}
                >
                  {`Add ${found.name} (${found.address})`}
                </ButtonItem>
              </PanelSectionRow>
            ),
          )}
          <PanelSectionRow>
            <ButtonItem layout="below" disabled={busy} onClick={onSearch}>
              {busy ? "Searching…" : "Search Again"}
            </ButtonItem>
          </PanelSectionRow>
        </>
      )}
    </PanelSection>
  );
}
