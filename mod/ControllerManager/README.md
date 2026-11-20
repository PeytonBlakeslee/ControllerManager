# Controller Manager 1.0.0

Customize Walking and Driving controller bindings, look sensitivity, and shortcuts for compatible SDK mods in Borderlands 2. The mod does not replace your keyboard mappings.

## Install or update

1. Exit the game. Back up `sdk_mods/ControllerManager/` and its saved settings (usually `sdk_mods/settings/ControllerManager.json`).
2. Extract the matching **Player ZIP** into `Borderlands 2/sdk_mods/`. Remove old numbered or duplicate installations; do not mix Developer files.
3. Confirm `sdk_mods/ControllerManager/__init__.py`, restart the game, and enable **Controller Manager** in the SDK Mods menu.

Keep old Controller Customizer and Controller Binds mods disabled; Controller Deadzone Control can remain installed separately. Restore your backup to roll back.

## Basic setup

- **Use Custom Bindings:** Off by default. Turn On for separate Walking and Driving controller layouts.
- **Auto-Swap Conflicts:** On by default. Reassigns a displaced action where possible instead of leaving it Unbound.
- **Look sensitivity:** Independent hip-fire, ADS, and vehicle horizontal/vertical controls, 0.00–5.00 in 0.05 steps (default 1.00).
- **Mod Shortcuts:** Assign compatible SDK actions to a controller button, with optional Start/Back modifier. Requires Manager Enabled, not Custom Bindings.

In the binding menu, use **A or Enter** to change a button, **X** to unbind, **Y** for Default, and **D-pad Left/Right** for sensitivity. Resets require confirmation.

## Compatibility and support

Windows Borderlands 2 with a local controller is the primary Player target. TPS is experimental and untested in-game; co-op host/client separation and Proton/Wine are unverified. Native Linux BL2 is unsupported by this package. Other mods' shortcuts retain their own gameplay restrictions.

For menu instructions, troubleshooting, and rollback, see the [Controller Manager user guide](https://github.com/PeytonBlakeslee/ControllerManager/blob/main/docs/USER_GUIDE.md).
