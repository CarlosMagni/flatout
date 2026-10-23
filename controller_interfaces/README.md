# Controller interfaces

Hardware on a controller socket of the CU that plays the hand controllers, so that RaceLab's
bots drive on any CU, including those that take no bot command (hardware 8).

Any device that speaks the small serial protocol in
[docs/controller-interface.md](../docs/controller-interface.md) works with RaceLab as it is: the
`C` frames in, an optional report line out, and a 750 ms expiry. That page also shows how to plug
in another transport with a few lines of Python.

| Folder | Hardware | Socket side | Link to RaceLab |
|---|---|---|---|
| [pico/](pico/README.md) | Raspberry Pi Pico (MicroPython) + NPN | socket 1, as a WIRELESS+ receiver, synced on the CU's burst | HC-05 Bluetooth serial |

To add one, put it in its own folder with a README that gives the wiring, the socket side and how
to check it, and add a row here.
