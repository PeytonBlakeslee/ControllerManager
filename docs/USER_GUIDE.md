# Controller Manager: User Guide

[Back to the repository overview](../README.md)

**Contents:** [Install, update or rollback](#install-update-or-rollback) · [Walking and Driving](#walking-and-driving) · [Menu controls and binding changes](#menu-controls-and-binding-changes) · [Mod shortcuts](#mod-shortcuts) · [Settings and resets](#settings-and-resets) · [Compatibility and limitations](#compatibility-and-limitations) · [Troubleshooting](#troubleshooting)

## Install, update or rollback

1. Confirm [PythonSDK and Willow2 Mod Manager](https://bl-sdk.github.io/willow2-mod-db/) work in Windows Borderlands 2.
2. Exit the game. Back up `sdk_mods/ControllerManager/` and `sdk_mods/settings/ControllerManager.json` (or your SDK's actual settings location).
3. Remove duplicate or older numbered Controller Manager installations. Do not run Controller Customizer or Controller Binds alongside Manager; Controller Deadzone Control is optional and separate.
4. Extract a matching **Player ZIP** so `sdk_mods/ControllerManager/__init__.py` exists. Do not install GitHub's source archive or mix Player and Developer files.
5. Launch the game and check **Enabled**, Walking/Driving layouts, sensitivities, and any Mod Shortcuts.

To **roll back**, exit the game and restore the backed-up folder and settings. An existing Manager configuration takes priority over legacy Controller Customizer values; old settings are not deliberately overwritten. Manager remembers its Enabled state once saved. A legacy configuration without that field can initially load disabled.

## Walking and Driving

Turn on **Use Custom Bindings** to activate separate layouts that switch automatically between walking and vehicle contexts. This option is **Off by default**. The game keeps its normal keyboard mappings.

1. Choose **Walking** or **Driving** from Controller Manager's menu.
2. Select a controller action to change its assigned physical button, set it to **Unbound**, or restore **Default**.
3. Adjust **Hip**, **ADS**, or **Look** horizontal/vertical sensitivity as needed.
4. Test the layout in game, then enter or exit a vehicle to check that the context changes correctly.

Sensitivity ranges from **0.00 to 5.00**, in **0.05** increments; default is **1.00**. The Driving panel describes stock vehicle actions. Custom vehicles and experimental TPS mappings may differ.

## Menu controls and binding changes

- Choose a category on the left; **A** or **D-pad Right** enters its right-hand list. Use Up/Down to navigate and **B** to return. **D-pad Left** also returns except when adjusting sensitivity.
- Controller binding row: **A or Enter** changes a button; **X** unbinds; **Y** restores Default.
- Sensitivity row: **D-pad Left/Right** adjusts the value directly.
- **Auto-Swap Conflicts** is **On by default**. If a new assignment displaces another action, Auto-Swap attempts to move that action to the old button; with Auto-Swap Off, the displaced action becomes Unbound.
- Layout and master resets ask for confirmation. The menu can remain accessible while Controller Manager's gameplay switch is Off.

Binding changes wait for a verified local input device and released buttons when required. If a controller appears stuck during capture, cancel the dialog, release all buttons, and reopen the menu.

## Mod shortcuts

Use **Mod Shortcuts** to bind available, rebindable actions exposed by compatible SDK mods. These shortcuts are separate from Walking and Driving remapping.

1. Open **Mod Shortcuts**, select the target mod and action, then choose **Change**.
2. Select **Start**, **Back**, or **X: No modifier** in the capture dialog. Choose a physical button when prompted; **Y** clears the assignment.
3. During play, hold Start or Back before pressing the assigned button for modifier shortcuts. An unmodified shortcut uses its assigned button alone.
4. Test the action with the target mod in a supported gameplay state. Press Start/Back on their own to confirm their ordinary menu behavior is retained.

Mod shortcuts require **Controller Manager Enabled**, but work with **Use Custom Bindings Off**. A shortcut uses the local controller event and takes precedence over a matching native button action. Duplicate assignments are rejected; unsupported actions are not listed. Shortcuts pause in menus, loading screens, and cinematics.

Only single buttons and Start/Back-plus-button combinations are supported, not arbitrary multi-button chords or analog stick axes. An action from a disabled mod works only if that mod explicitly keeps its public keybind active. Shortcuts do not bypass host-only/co-op restrictions of other mods.

## Settings and resets

- **Reset Walking Settings / Reset Driving Settings:** Restore that layout's bindings and sensitivities after confirmation.
- **Reset [mod] Bindings:** Clear shortcuts for one compatible mod.
- **Reset All Controller Settings:** Restore both layouts, look sensitivity, and SDK mod shortcuts after confirmation. Keyboard mappings are unaffected.
- **Retry Pending Binding Restore:** When Controller Manager is Off, retry a previously interrupted binding restoration after releasing gameplay buttons. If input callbacks are no longer running, restart the game instead.

Saved Controller Manager settings live in `ControllerManager.json`. Native binding capture and menu display can remain available while gameplay bindings are disabled.

## Compatibility and limitations

| Environment | Status |
| --- | --- |
| Windows Borderlands 2, local controller | Primary Player target |
| Co-op host/client controls | Intended to be local; two-system behavior not fully verified |
| The Pre-Sequel | Experimental; controller/menu/vehicle behavior not gameplay-certified |
| Windows game through Proton/Wine | Unverified, including Steam Input mapping |
| Native Linux Borderlands 2 | Unsupported by this Windows package |

Controller Deadzone Control is optional. The target SDK mod controls whether an assigned shortcut is meaningful for a joining client. TPS vehicle labels are descriptive and do not establish exact live bindings.

## Troubleshooting

- **Manager not listed:** Check PythonSDK/Willow2 and `sdk_mods/ControllerManager/__init__.py`. Remove old numbered or nested duplicates.
- **Bindings do not apply:** Confirm Controller Manager is Enabled, **Use Custom Bindings** is On, and held buttons have been released.
- **Shortcut not responding:** Check that its target mod exposes the action and is in a supported state. Cancel capture dialogs, release held buttons, and check for binding conflicts.
- **Settings missing:** Exit the game and restore `ControllerManager.json` from your backup. Avoid mixing old Customizer files with the current Manager configuration.
- **Input remains stuck after disable or travel:** Exit or cancel menus, release the controller buttons, and try the **Retry Pending Binding Restore** option with Manager Off. Restart if input processing cannot recover.

[Report an issue](https://github.com/PeytonBlakeslee/ControllerManager/issues) with game/SDK/mod versions, controller model, controller/Steam Input configuration, Walking or Driving context, reproduction steps, and a short relevant `unrealsdk.log` excerpt. Redact private paths, account details, and save data.
