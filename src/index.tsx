import { definePlugin, useQuickAccessVisible } from "@decky/api";
import { staticClasses } from "@decky/ui";
import { useCallback, useEffect, useState } from "react";
import { FaPowerOff } from "react-icons/fa";
import { getState, refreshState, subscribeState } from "./api";
import { MachineView } from "./MachineView";
import { SettingsView } from "./SettingsView";
import type { MachineSnapshot } from "./types";

const INITIAL_SNAPSHOT: MachineSnapshot = {
  configured: false,
  paired: false,
  machine: {
    id: "",
    name: "",
    host: "",
    daemon_port: 9777,
    mac: "",
    broadcast: "255.255.255.255",
    wol_port: 9,
    delay_s: 15,
  },
  selected_id: "",
  machines: [],
  attention: null,
  reachable: false,
  error: null,
  seen_at_unix_ms: null,
  server_now_unix_ms: Date.now(),
  phase: "checking",
  action: null,
  deadline_unix_ms: null,
  last_seen_unix_ms: null,
  offers: ["wake"],
  waiting: false,
  label: "Checking…",
  poll_interval_ms: 10_000,
};

function Content() {
  const [snapshot, setSnapshot] = useState<MachineSnapshot>(INITIAL_SNAPSHOT);
  const [view, setView] = useState<"main" | "settings">("main");
  const [nowMs, setNowMs] = useState<number>(() => Date.now());
  const qamVisible = useQuickAccessVisible();

  const applySnapshot = useCallback((next: MachineSnapshot) => {
    setSnapshot(next);
    setNowMs(Date.now());
  }, []);

  useEffect(() => {
    let active = true;
    void getState().then((next) => {
      if (active) applySnapshot(next);
    });
    const unsubscribe = subscribeState((next) => {
      if (active) applySnapshot(next);
    });
    return () => {
      active = false;
      unsubscribe();
    };
  }, [applySnapshot]);

  useEffect(() => {
    if (!qamVisible) return;
    void refreshState().then(applySnapshot).catch(() => {});
  }, [qamVisible, applySnapshot]);

  useEffect(() => {
    if (!qamVisible) return;
    const timer = window.setInterval(() => {
      setNowMs(Date.now());
    }, 1000);
    return () => {
      window.clearInterval(timer);
    };
  }, [qamVisible]);

  if (view === "settings") {
    return (
      <SettingsView
        snapshot={snapshot}
        onUpdated={applySnapshot}
        onBack={() => setView("main")}
      />
    );
  }

  return (
    <MachineView
      snapshot={snapshot}
      nowMs={nowMs}
      onUpdated={applySnapshot}
      onOpenSettings={() => setView("settings")}
    />
  );
}

export default definePlugin(() => {
  return {
    name: "Barge",
    titleView: <div className={staticClasses.Title}>Barge</div>,
    content: <Content />,
    icon: <FaPowerOff />,
  };
});
