# bot — the bot module

Software players on CU slots 0-5, driven from the PC over the AppConnect dongle.
Built by [`../build.py`](../build.py) into the stock V339 image.

| file | role |
|---|---|
| `bot_mode.c` | the module: parses `B` commands, rewrites the controller words of bot slots |
| `stubs.S` | the three entry points the hooks jump to |
| `event-ring-incident.md` | why bot.2 was defective (RAM `0x00E9` is the event ring) |

## Serial contract

```
`B<addr><v>$   v = '0'..'9', ':', ';', '<', '=', '>', '?'  → bot throttle 0-15
               v = 'A'  → hand the slot back to the physical controller
               v = 'L'  → one-shot lane change (switch "pressed" for one word)
```

Full specification on the app side: [`docs/bot-protocol.md`](../../docs/bot-protocol.md).
Without any `B` command the module returns the words unchanged: same behaviour as
the stock firmware.

## How it hooks in

| hook | stock site | replaces | goes to |
|---|---|---|---|
| P1 | `0x05FC` | serial ISR error path (8 bytes) | `STUB1`: accept `B` as a command letter, else replay the error path |
| P2 | `0x06E6` | dispatcher `sts 0x00E6,r1` (4 bytes) | `STUB2`: route `B` to the module, else replay |
| P3 | `0x304A` | the two `sts` to the TX shifter (8 bytes) | `STUB3`: filter the controller word before it is sent (a `jmp`, the stack is untouched) |

## Memory map

- **Flash**: module at `0x36B8-0x37EF` (296 of 312 free bytes used). The id string
  at `0x37F0` and the bootloader at `0x3800+` are never touched.
- **RAM**: state in bytes no firmware function uses — slot structs at
  `0x21C + 16·i`: throttle in byte [2] (`0x21E`, `0x22E`, …), bot mask in slot 0
  byte [3] (`0x21F`), lane-change mask in slot 1 byte [3] (`0x22F`). Zeroed at
  reset by crt0. **Never use `0x00E9-0x0118`** (event ring).
- No `.data`/`.bss`, no division helper: `build.py` refuses a module that needs them.

## Controller word (what P3 filters)

9 bits, sent bit 0 first: bits 0-2 car address (untouched), bit 3 SW (0 = pressed),
bits 4-7 throttle with reversed bit order (bit 4 = G3), bit 8 TA (fuel mode, CU).
Only word ids 0-5 are controllers.
