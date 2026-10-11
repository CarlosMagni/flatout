# Changelog — firmware images

SHA-256 of the `.HMF` file. Full list in `manifest.json`. Images are built from source by `build.py`.

## v339-bot.4 — recommended

`1657008f732b8bda2e3f1a6f2807b52e59a71b356c9151cf9f6f80768f7f56a4`

- Same bot module source as bot.3, built with the pinned toolchain (avr-gcc 5.4.0) on the
  stock image rebuilt from the C reconstruction: one compiler for the whole firmware.
  Module 294 bytes (296 with 9.5.0), same hooks and stub addresses.

## v339-bot.3 — superseded

`0ee3eb1a6753dc45e4a09f58b56acca8d0e526c5cf44c9f1fbcca0976ca62c9d`

- Bot state moved to free bytes of the slot structs (`0x21C + 16·i + 2`, masks at
  `0x21F`/`0x22F`). Fixes bot.2.
- Built with avr-gcc 9.5.0 (no longer built; still recognised by its SHA-256).

## v339-bot.2 — defective, never flash

`35f452f432cd1ee2443c7103d4ec49a3c91cdd2ac8cab247a40b1bb06bec71e6`

- Bot state at `0x00E9-0x00F2` overlapped entry 0 of the firmware's event ring
  (`0x00E9-0x0118`): corrupted throttle sent to the cars, wrong `?` status frames.
  See [`bot/event-ring-incident.md`](bot/event-ring-incident.md).

## v339-bot.1 — defective, never flash

`2e21d0c8ebcd8c6b433dea05328bd9d94e253329b1a0fb6e3847f773efbf1a79`

- The hook wrote the throttle over the controller word's address bits: the bots'
  cars did not stop.

## v339-stock

`c1af1e5a3ea9a2d3590fdff94f49bb7409be63e66729813c9ff2043ecd2b608e`

- Stock V339, rebuilt from the C reconstruction (byte-identical), encoded with
  fixed-length lines; same payload as the vendor update file (`v339-stock-vendor`, `843ae3d8…`).
