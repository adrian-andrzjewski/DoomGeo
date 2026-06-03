# DoomGeo-MVS Build and Release Plan

This file is the committed plan tracked by `doomgeo-plan`. It is intentionally
separate from the build helper so one standalone binary can build/package while
the other only accompanies and tracks the plan.

## Checklist

- [x] Add a plan-only CLI that reads and updates this markdown checklist.
- [x] Add a build CLI with doctor, build, package, install-tools, and uninstall commands.
- [x] Package both CLIs as standalone Linux and Windows binaries in GitHub Actions.
- [x] Build the Neo Geo ROM in GitHub Actions on Ubuntu 24.04.
- [x] Add a GitHub Pages bundle that plays the ROM through a WebAssembly/asm.js browser emulator frontend.
- [ ] Add a fully native Windows/MSYS2 ROM build job after validating ngdevkit UCRT64 in CI.
- [ ] Add signed release uploads for tagged builds.
- [ ] Add a smoke-run screenshot capture job for the Linux ROM build.
- [ ] Decide whether the final user-facing build helper should install ngdevkit through MSYS2, WSL, Docker, or all three on Windows.

## Evidence

- Linux ROM builds are expected to produce `build/rom/puzzledp.zip`.
- Standalone helper builds are expected to produce `doomgeo-build` and
  `doomgeo-plan` artifacts for Linux, plus `.exe` variants for Windows.
- The Pages bundle is expected to publish `index.html`, `rom/puzzledp.zip`,
  and `rom/neogeo.zip`.
- Repo-local installs are removable with `doomgeo-build uninstall`; `--all`
  also removes cached WAD/package downloads under `.tools`.
