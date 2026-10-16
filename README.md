# LSWTSS Level Exporter — version 0.1.0 (first release, early preview)

Exports maps from **LEGO Star Wars: The Skywalker Saga** (PC) into Blender `.blend` files: the placed meshes, rebuilt materials with their textures packed inside the file, the map's sky and its authored lights. It reads your own installed copy of the game; **no game files are included** with this tool.

## What you need

- Windows 10 or 11 (64-bit).
- LEGO Star Wars: The Skywalker Saga installed on PC (Steam is detected automatically; any copy works if you browse to its folder — the one containing `GAME.DAT`).
- **Blender**, free from [blender.org](https://www.blender.org/download/). Tested with Blender 5.2; 4.2 or newer is required.
- Disk space: the first export unpacks game files into a cache (several GB, grows as you export more maps). A finished map is typically 100 MB – 1 GB.

You do **not** need Python or .NET installed.

## How to use it

1. Unzip the folder anywhere and run **`LSWTSS Level Exporter.exe`**.
2. Check the **Game folder** and **Blender** paths (filled in automatically when found).
3. Choose an **Output folder** (default `Documents\LSWTSS Exports`).
4. Click **Load level list**. The first time this indexes the game archives and takes a few minutes.
5. Pick a map (type in **Search** to filter, e.g. `kachirho` or `federal`) and click **Export selected level**.
6. Wait. Small maps take a few minutes; big hub planets can take 15–30 minutes and need plenty of RAM (16 GB+ recommended for the largest).
7. Open the `.blend` from the output folder. Use **Material Preview** to look around; render with **Cycles** for the game-like lighting.

Windows SmartScreen may warn that the app is unrecognised (it is not code-signed). Choose **More info → Run anyway** only if you got this copy from a source you trust.

## What gets exported

- Static map geometry: models, GSC objects (LEGO builds and props), painted foliage, terrain, projected decals, glow strips/panels, streamed hub puzzle props (initial state).
- Per-placement detail: material overrides and painted vertex colours.
- Materials translated from the game's own shader graphs (all 6,103 game materials compile; ~84% match the game shaders exactly, the rest approximate a few effects — see below).
- HDR sky as the World background, and the game's placed lights.
- Editor helpers, collision and shadow-only meshes are hidden.

Each `.blend` contains two text blocks (Blender's Text Editor): **Level Export Report.json** and **Material Translation Report.json**, listing anything that was skipped or approximated.

## Known limitations (version 0.1.0)

- **Not exported yet:** characters/NPCs, particle effects (fire, smoke, sparks), water surfaces, sand/snow/mud weathering on props, destructible/alternate states.
- **Approximated:** wind sway (rest pose), close-up parallax (within ~4 m), soft fog edges, screen-space glass distortion, leaf translucency, live in-game screens (TVs/terminals show black).
- Only a handful of maps have been checked visually so far; other maps may show problems. Please report them with the map name and a screenshot.
- Lighting is the game's placed lights and sky rendered by Cycles, not the game's baked lightmaps, so it can differ from the game.

## Credits and licences

- Game archive reading uses the BrickVault TTGames DAT reader (C#), built into `ArchiveReader.exe`.
- Oodle decompression uses `oo2core_8_win64.dll` **from your own game installation**; it is not distributed with this tool.
- Built with Python, Pillow, NumPy and PyInstaller.
- LEGO, Star Wars and The Skywalker Saga are trademarks of their respective owners. This is an unofficial fan tool, not affiliated with or endorsed by TT Games, LEGO, Lucasfilm or Warner Bros.

## Download

Get the ready-to-run Windows build from the [Releases page](../../releases): download `LSWTSS_Level_Exporter_v0.1.0_win64.zip`, unzip, and run `LSWTSS Level Exporter.exe`.

## Building from source

- `exporter_app.py` — the desktop app (Tkinter). `exporter_app.py --prepare …` runs the data stage.
- `prepare_level.py` — reads the game archives, decodes scenes, models, GSC objects, terrain, foliage, decals and materials, and writes the build manifests.
- `build_blend.py` + `lib/lswtss_native_lod0/` — runs inside Blender to assemble the scene and compile the materials.
- The archive reader (`ArchiveReader.exe`, BrickVault-based, .NET 10 self-contained) is not part of this source tree; it ships inside the release zip.

Packaged with PyInstaller (`--windowed`, adding `build_blend.py`, `lib/`, `light_schema.json`, `prepare_level.py` and `ArchiveReader.exe` as data).
