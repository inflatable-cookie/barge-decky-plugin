# Prepare the plugin to go public

- Scrubbed Tom-specific values from the tracked tree: the HMAC/WoL
  vectors file names the Barge repo URL instead of a local checkout path,
  test fixtures use neutral `pc` / `pc.local` names with RFC 5737
  documentation addresses, the manual-add placeholders match, and the
  `bazzite` tag is gone from `plugin.json`.
- Rewrote the README for a public reader: what the plugin does, what it
  needs (with Barge's `barged` install guide), LAN discovery and pairing,
  installing from a release zip, building from source, pre-1.0 status, and
  the licence. Owning details stay linked, not restated.
- Made the MIT copyright line match Barge's (`Copyright (c) 2026 Tom
  Wright`).
