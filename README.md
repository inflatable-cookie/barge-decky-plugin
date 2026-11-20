# Barge Decky Plugin

Control a PC running [`barged`](https://github.com/inflatable-cookie/barge/blob/main/docs/knowledge/machines.md) from Steam Deck Gaming Mode: view live power state, wake via Wake-on-LAN, trigger Sleep, Restart, or Shut down countdowns, and cancel pending actions.

## Features

- **Several machines:** a dropdown at the top of the Quick Access panel lists each configured machine with its state, and remembers which one you chose.
- **State-driven Quick Access panel:** shows only valid actions for the observed state (`Offline` / `Checking…` -> Wake; `Awake` -> Sleep, Restart…, Shut down…; `Pending` -> countdown and Cancel; waiting states fast-poll every 2 s). An unreachable machine is simply Offline; problems that need you (such as a rejected LAN key) show one line with a link to the error log.
- **Error log:** the icon button at the bottom of the panel lists this session's machine errors, newest first (last 50, not saved across restarts).
- **Wake-on-LAN:** sends a 102-byte UDP broadcast magic packet from the Steam Deck backend even when the PC and `barged` are down.
- **LAN HMAC authentication:** signs requests with SHA-256 HMAC (`x-barge-timestamp`, `x-barge-nonce`, `x-barge-signature`) using the shared 256-bit key from `barged pair`.
- **Owner-only key storage:** stores each machine's LAN key in its own `0o600` file, separate from the machine list, inside Decky's plugin settings directory.

## Install (Decky Developer Mode)

1. Build or download `barge-decky-plugin-v<version>.zip` (see **Build and Package** below).
2. On the Steam Deck in Gaming Mode, open **Quick Access (`...`) -> Settings -> System** and enable **Developer Mode** (under Decky settings if needed).
3. Open **Decky Settings -> Developer -> Install Plugin from ZIP** (or **Install Plugin from URL**).
4. Select `barge-decky-plugin-v0.2.2.zip` and confirm installation.

## Configure and Pair

1. Ensure `barged` is running on the target PC in LAN mode (`mode: "lan"` in `barged`'s config, default port `9777`).
2. On the target PC, generate a pairing key:
   ```bash
   barged pair --config /etc/barged/config.json
   ```
   Copy the 64-character lowercase hex key it prints (or save it to a temporary file transferred to the Deck).
3. In Steam Deck Gaming Mode, open **Quick Access -> Decky -> Barge -> Settings**. Choose **Add machine…** from the machine list, then fill in:
   - **Machine Name:** friendly label for your PC (e.g. `bazzite`). Names must be unique.
   - **Host / LAN Address:** hostname or LAN IP of the PC (e.g. `bazzite.local` or `192.168.1.50`).
   - **Daemon Port:** `9777` (default).
   - **MAC Address:** PC Ethernet/Wi-Fi MAC address for Wake-on-LAN (e.g. `00:11:22:33:44:ff`).
   - **Broadcast Address:** `255.255.255.255` (or your subnet broadcast, e.g. `192.168.1.255`).
   - **Wake-on-LAN Port:** `9` (default).
   - **Countdown Delay:** `15` seconds (default).
   - **LAN Key:** paste the 64-character hex key or click **Import Key File…** to pick the key file via the SteamOS file picker.
4. Select **Add Machine** (or **Save Machine** when editing). The machine you add becomes the one on the panel. Add more machines the same way; each has its own key.

If you installed v0.1.0, your machine and key are carried over automatically on first load.

## Build and Package

Requires Node.js (v18+), `pnpm` v9, and Python 3.9+ (`pytest` for tests).

```bash
pnpm install --frozen-lockfile
pnpm run typecheck
pnpm run build
pnpm run package
```

Run backend unit tests:

```bash
pytest
```

The distributable archive is written to `out/barge-decky-plugin-v0.2.2.zip`.
