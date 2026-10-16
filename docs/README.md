# RaceLab documentation

| Document | For | Content |
|---|---|---|
| [getting-started.md](getting-started.md) | everyone | install, first launch, first race ([français](fr/getting-started.md)) |
| [user-guide.md](user-guide.md) | users | the app in detail: modes, screens, bot safety, firmware update, development |
| [api.md](api.md) | agent and script authors | WebSocket / REST API, track connectors, Python client |
| [architecture.md](architecture.md) | contributors | modules and their contracts |
| [protocol.md](protocol.md) | protocol / tool authors | CU serial and Bluetooth protocol |
| [cu-firmware-versions.md](cu-firmware-versions.md) | firmware and app authors | what changed from V336 to V339, which versions take bots without flashing |
| [bot-protocol.md](bot-protocol.md) | firmware and app authors | the `C` (stock CU) and `B` (bot module) commands for software drivers, and their safety rules |
| [hardware-test.md](hardware-test.md) | testers | the first field test: `C` command, camera, map autopilot, with go/no-go numbers ([français](fr/hardware-test.md)) |
| [firmware/](../firmware/README.md) | firmware contributors | images and tags, building the bot image, the C reconstruction |

Also: [examples for agents](../racelab/examples/README.md) (Python client,
Gymnasium environment, a track connector) and the
[tools](../tools/README.md). The project overview is in the
[main README](../README.md); see [CONTRIBUTING](../CONTRIBUTING.md) to help.
- [roadmap.md](roadmap.md): status, phases, what is left
