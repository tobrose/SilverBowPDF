# Packaging and distribution

## One-time setup

```powershell
winget install JRSoftware.InnoSetup
```

## Build

```powershell
.\packaging\build.ps1
```

Output: `packaging\out\SilverbowPDF-Setup-<version>.exe`, the version
taken from `pdfapp/__init__.py`. Double-click, Next,
Install. For deployment tooling: `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART`.

## Why these tools

**PyInstaller onedir, not onefile.** onefile unpacks the whole Qt runtime to
`%TEMP%` on every single launch. That means multi-second startup, and on a
managed Windows box it looks indistinguishable from malware staging itself:
AppLocker blocks it, Defender rescans ~200MB per launch. onedir installs once
and starts immediately.

**Inno Setup, not MSI.** Inno produces an `.exe`, and endpoint-management
tools deploy an `.exe` with silent switches perfectly well. Go to WiX/MSI
only if you need GPO software installation policy specifically, which wants
a real MSI.

**No UPX.** Compressed executables are a heuristic AV trigger. Saving 30MB is
not worth arguing with Defender.

## Two gotchas already handled in the spec

Both were verified by reading the installed packages, not inferred:

1. **pdfium lives in `pypdfium2_raw`,** a separate distribution from
   `pypdfium2`. PyInstaller's analysis does not follow the ctypes load.
2. **Both `pypdfium2` and `pypdfium2_raw` read a `version.json`** relative to
   their own `__file__` at import time. Miss either and the frozen app dies on
   `import pypdfium2` with a `FileNotFoundError`. This failure only appears on
   a machine with no Python installed — i.e. a user's machine, after you ship.
   The `build.ps1` smoke test exists specifically to catch it before then.

If the frozen app dies on launch, flip `console=False` to `console=True` in
`packaging/pdfapp.spec` and run the exe from a terminal. Without that you get a
window that never appears and no traceback anywhere.

## Code signing — read before distributing

The installer this produces is **unsigned**. On any machine with default
settings, SmartScreen shows "Windows protected your PC" with the Run-anyway
button hidden behind **More info**.

For your own machine, ignore this. For anyone else, understand what you're
choosing between:

- **Don't sign, deploy silently instead.** If you manage the endpoints, push
  the installer with your endpoint-management tool and users never see a
  SmartScreen prompt, because they never run the installer — the tool does,
  as SYSTEM. Add a Defender/ASR exclusion for the install path if needed.
  Cost: zero. Usually the right answer for internal distribution.
- **OV certificate,** roughly $200–400/year. Removes the "unknown publisher"
  wording, but SmartScreen reputation still accrues per-certificate over time,
  so early builds may still warn. Needed if it's distributed outside your
  organisation.
- **EV certificate,** more expensive and requires a hardware token. Immediate
  SmartScreen reputation.

Signing is not a checkbox you add later for free — every release has to be
signed, so it becomes part of the build pipeline permanently. Decide before you
build the habit, not after.

## Distribution paths, in preference order

**1. An endpoint-management tool, for physical machines.** Wrap the setup exe
in a deployment with `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART` and a
detection rule on the uninstall registry key so it's idempotent, and it's
zero clicks for the user — which is strictly better than one. Upgrades work
because `AppId` in `installer.iss` is fixed: Inno recognises the existing
install and replaces it in place.

**2. The master image, for non-persistent virtual desktops.** On a desktop
that resets at logoff an installer is the wrong artifact entirely — what it
installs evaporates. The app goes into the master image, which means it
follows that image's release cadence and change process: keep those desktops
on a stable tagged release and iterate against physical machines.

On any remote desktop, pages are rendered as bitmaps that are then
re-encoded and sent over the display protocol, so scrolling a 600 DPI scan at
400% zoom feels different there than it does locally. Test on the real
environment before committing, not on your desk.

**3. A file share, for you and a couple of pilot users.** Fastest loop while
the thing is still moving. Don't scale this past a handful of people — there's
no update mechanism and you'll end up supporting four versions at once.

## Default PDF app

The installer never makes SilverbowPDF the default for `.pdf`: Windows 10/11
ignore an installer doing that (the user's choice is hash-protected). It
registers the app as a PDF handler instead, so it appears under *Open with*
and in *Settings > Apps > Default apps*, and uninstalling removes only its own
entries.

To make it the default for everyone, use Windows' default-associations file:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<DefaultAssociations>
  <Association Identifier=".pdf" ProgId="SilverbowPDF.Document" ApplicationName="SilverbowPDF" />
</DefaultAssociations>
```

- **GPO:** Computer Configuration > Administrative Templates > Windows
  Components > File Explorer > *Set a default associations configuration
  file*, pointing at that XML on a share. Applied at each logon.
- **In an image:** `Dism /Online /Import-DefaultAppAssociations:C:\path\assoc.xml`
  applies to profiles created afterwards; users whose profiles roam or
  persist keep their own choice -- use the GPO for them.

Upgrades start from a clean bundle: the installer deletes the previous
version's `_internal` folder first (`[InstallDelete]`), so files a newer build
dropped don't linger.

## Administrator policy

Settings users choose live in `HKCU\Software\SilverbowPDF`; policy set by IT
lives in `HKLM\Software\Policies\SilverbowPDF` and wins, with the matching
menu item shown as "set by administrator". On non-persistent desktops policy
is the reliable place: HKLM is part of the machine image, while HKCU may not
survive logoff.

| Value | Type | Meaning |
|---|---|---|
| `SingleKeyShortcuts` | DWORD | `0` locks single-letter tool shortcuts (V, H, T, ...) off; `1` locks them on. Absent: off by default, and users may turn them on in the Tools menu. Ctrl shortcuts are never affected. |

Set it by GPO (Registry preference) or any endpoint-management tool. The
installer doesn't set it: the shortcuts are off by default, which is usually
all that's needed.
