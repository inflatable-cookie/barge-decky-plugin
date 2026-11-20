# Barge Decky Plugin

`inflatable-cookie/barge-decky-plugin` is a Decky Loader plugin that controls a Barge machine running `barged` from Steam Deck Gaming Mode.

## Protocol Ownership

[Barge](https://github.com/inflatable-cookie/barge) owns the `barged` HTTP protocol, LAN HMAC request signing, Wake-on-LAN format, state model, and UI wording:

- [Machines design and protocol](https://github.com/inflatable-cookie/barge/blob/main/docs/knowledge/machines.md)
- [Barge app interaction rules and wording](https://github.com/inflatable-cookie/barge/blob/main/docs/knowledge/app.md)
- [barge-machine Rust reference client and signer](https://github.com/inflatable-cookie/barge/tree/main/crates/barge-machine)
- [Quality rubric](https://github.com/inflatable-cookie/barge/blob/main/docs/knowledge/contracts/quality.md)

Do not invent new daemon endpoints, schemas, or state names here. Keep the Python signing and state model byte-for-byte compatible with `barge-machine` and `machine_state.rs`.

## Quality Gate and Guardrails

- Hold every part to 8/10 or higher on Barge's [quality rubric](https://github.com/inflatable-cookie/barge/blob/main/docs/knowledge/contracts/quality.md).
- All daemon HTTP requests and Wake-on-LAN packets originate from the Python backend (`main.py` and `py_modules/barge_plugin/`), never from the frontend.
- Keep transport and auth behind the `HttpTransport` / `AuthStrategy` seams in `py_modules/barge_plugin/transport.py` so Tailscale mode can be added without rewriting the client or state tracker.
- Store each machine's LAN key in its own file, `keys/<machine-id>.key`, inside `DECKY_PLUGIN_SETTINGS_DIR`, separate from the machine list (`machines.json`), with `0o600` permissions. Never log or return key material.
- Never commit secrets, real host details, or personal information.
- Add one changelog fragment under `changelog.d/<task-or-pr>-<slug>.md` per PR; keep `CHANGELOG.md`'s `[Unreleased]` section empty.

## Layout

- `main.py` — thin Decky Loader `Plugin` entrypoint (`api_version: 1`).
- `py_modules/barge_plugin/` — pure-stdlib Python backend: `auth.py`, `wol.py`, `protocol.py`, `transport.py`, `state.py`, `config.py`, `fs_atomic.py`, `service.py`, `errors.py`, `error_log.py`.
- `src/` — TypeScript/React frontend using `@decky/ui` and `@decky/api`.
- `tests/` — `pytest` suite covering Rust-generated HMAC/WoL vectors, state transitions, timeouts, loopback HTTP client, config/key isolation, and async service behavior.
- `scripts/package.sh` — builds the distributable plugin zip under `out/`.

## Validate

- Backend tests: `pytest` (using Python 3.9+ with `pytest` installed; no runtime pip dependencies required).
- Frontend typecheck: `pnpm run typecheck` (`tsc --noEmit`).
- Frontend bundle: `pnpm run build` (`rollup -c`, writes `dist/index.js`).
- Distributable zip: `pnpm run package` (`bash scripts/package.sh`, writes `out/barge-decky-plugin-v<version>.zip`).
