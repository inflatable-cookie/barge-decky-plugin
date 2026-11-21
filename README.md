# Barge Decky Plugin

Control a PC running `barged` from Steam Deck Gaming Mode: see its live
power state, wake it with Wake-on-LAN, and trigger Sleep, Restart, or Shut
down countdowns (or cancel a pending one).

The `barged` HTTP protocol, request signing, Wake-on-LAN format, state
model, and UI wording are owned by [Barge](https://github.com/inflatable-cookie/barge)
— see its [machines design and protocol](https://github.com/inflatable-cookie/barge/blob/main/docs/knowledge/machines.md)
and [app interaction rules](https://github.com/inflatable-cookie/barge/blob/main/docs/knowledge/app.md).
This plugin keeps its Python signing and state model byte-for-byte
compatible with Barge's `barge-machine` reference client.

## What you need

- A Steam Deck with [Decky Loader](https://decky.xyz) installed.
- A PC running `barged`. Follow Barge's [`barged` install guide](https://github.com/inflatable-cookie/barge/blob/main/docs/install-barged.md)
  to install it and put it in LAN mode.

## Discovery and pairing

Discovery and pairing are LAN-only. Open the plugin's Settings page and it
browses `_barged._tcp` over mDNS for nearby machines — tap **Add** on yours.
The plugin shows a six-digit code; approve the matching request on the PC
and the Deck gets its own pairing key, stored owner-only (`0o600`) on the
Deck. If discovery can't see the PC, add it by hand and paste a pairing key
instead.

## Install from a release

1. Download `barge-decky-plugin-v<version>.zip` from the
   [releases page](https://github.com/inflatable-cookie/barge-decky-plugin/releases).
2. On the Deck in Gaming Mode, enable Developer Mode (Quick Access `...` →
   Settings → System, under Decky settings if needed).
3. Use Decky Settings → Developer → Install Plugin from ZIP and pick the zip.

## Build from source

Requires Node.js (v18+), `pnpm` v9, and Python 3.9+ (`pytest` for tests).

```bash
pnpm install --frozen-lockfile
pnpm run typecheck
pnpm run build
pnpm run package
```

Backend tests:

```bash
pytest
```

The distributable archive is written to
`out/barge-decky-plugin-v<version>.zip`.

## Status and licence

Pre-1.0: expect rough edges and report them as
[issues](https://github.com/inflatable-cookie/barge-decky-plugin/issues).
MIT — see [LICENSE](LICENSE).
