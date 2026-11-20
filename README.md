# Barge Decky Plugin

Control a PC running [`barged`](https://github.com/inflatable-cookie/barge/blob/main/docs/knowledge/machines.md) from Steam Deck Gaming Mode: view live power state, wake via Wake-on-LAN, trigger Sleep, Restart, or Shut down countdowns, and cancel pending actions.

## Features

- **Find and pair with nothing typed:** the Settings screen browses `_barged._tcp` over mDNS (its own browser, because SteamOS does not resolve `.local` names), lists the machines it finds with an **Add** button, and pairs with a six-digit code you approve on the PC.
- **Approvals on the PC:** when `barged` answers on localhost, the plugin treats that machine as this PC and shows pending requests ("Steam Deck wants to pair", the code, **Approve** / **Deny**) in the Quick Access panel.
- **Paired clients:** Settings lists a machine's paired clients and can revoke one; each client gets its own key.
- **Several machines:** a dropdown at the top of the Quick Access panel lists each configured machine with its state, and remembers which one you chose.
- **State-driven Quick Access panel:** shows only valid actions for the observed state (`Offline` / `Checking…` -> Wake; `Awake` -> Sleep, Restart…, Shut down…; `Pending` -> countdown and Cancel; waiting states fast-poll every 2 s). An unreachable machine is simply Offline; problems that need you (such as a rejected LAN key) show one line with a link to the error log.
- **Error log:** the icon button at the bottom of the panel lists this session's machine errors, newest first (last 50, not saved across restarts).
- **Wake-on-LAN:** sends a 102-byte UDP broadcast magic packet from the Steam Deck backend even when the PC and `barged` are down.
- **Per-client LAN HMAC authentication:** signs requests with SHA-256 HMAC (`x-barge-timestamp`, `x-barge-nonce`, `x-barge-signature`) using this Deck's own 256-bit pairing key, sent with `x-barge-client-id`.
- **Owner-only key storage:** stores each machine's LAN key in its own `0o600` file, separate from the machine list, inside Decky's plugin settings directory.

## Install (Decky Developer Mode)

1. Build or download `barge-decky-plugin-v<version>.zip` (see **Build and Package** below).
2. On the Steam Deck in Gaming Mode, open **Quick Access (`...`) -> Settings -> System** and enable **Developer Mode** (under Decky settings if needed).
3. Open **Decky Settings -> Developer -> Install Plugin from ZIP** (or **Install Plugin from URL**).
4. Select `barge-decky-plugin-v0.3.0.zip` and confirm installation.

## Find, Pair and Approve

1. Ensure `barged` is running on the target PC in LAN mode (`mode: "lan"` in `barged`'s config, default port `9777`). It advertises `_barged._tcp` when Avahi is available.
2. Open **Quick Access -> Decky -> Barge -> Settings**. The **Found on Your Network** section searches as soon as it opens; tap **Search Again** if the PC is not listed yet.
3. Tap **Add** next to the machine. The plugin opens a pairing request and shows the six-digit code while it waits.
4. Approve the request on the PC, where the same code is shown:
   - **Gaming Mode:** the Barge plugin's Quick Access panel shows *Steam Deck wants to pair* with **Approve** and **Deny**.
   - **Desktop Mode:** `barged`'s desktop notification offers the same actions.
   - A terminal fallback is `barged pair --approve`.
5. Once approved, the plugin stores its own key in `0o600` `keys/<machine-id>.key`, reads the machine's stable ID, MAC address and broadcast address from `GET /v1/state`, and selects the machine on the panel.

Saved machines stay out of **Found on Your Network**. Search follows changes to their addresses using the daemon ID, then the MAC, IP address or mDNS hostname for older entries. A successful poll fills in missing machine details.

### Manual fallback

If discovery is unavailable, choose **Add machine…** from the machine dropdown and fill in the details by hand. The **Manual Pairing Key (Fallback)** section still accepts a pasted 64-character hex key or an imported key file (for example from `barged pair`).

### Paired clients

With a paired machine selected, **Paired Clients** lists every client `barged` has registered and can revoke one. Revoking this Deck's own client removes its access until it pairs again.

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

The distributable archive is written to `out/barge-decky-plugin-v0.3.0.zip`.
