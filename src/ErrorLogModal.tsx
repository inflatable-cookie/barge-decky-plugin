import { DialogHeader, Field, ModalRoot } from "@decky/ui";
import { formatLogTime } from "./model";
import type { ErrorLogEntry } from "./types";

interface ErrorLogModalProps {
  entries: ErrorLogEntry[];
  closeModal?(): void;
}

/** Session-only error log, newest first. Nothing here is stored across restarts. */
export function ErrorLogModal({ entries, closeModal }: ErrorLogModalProps) {
  return (
    <ModalRoot closeModal={closeModal} onCancel={closeModal}>
      <DialogHeader>Error log</DialogHeader>
      <div style={{ maxHeight: "60vh", overflowY: "auto" }}>
        {entries.length === 0 ? (
          <Field
            label="No errors this session"
            description="Problems with a machine are listed here, newest first."
          />
        ) : (
          entries.map((entry, index) => (
            <Field
              key={`${entry.at_unix_ms}-${index}`}
              label={`${formatLogTime(entry.at_unix_ms)} · ${entry.machine_name || "Barge"}`}
              description={entry.message}
              bottomSeparator="standard"
            />
          ))
        )}
      </div>
    </ModalRoot>
  );
}
