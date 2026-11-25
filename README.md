# DDON Reborn Motion Library (Beta)

A Windows desktop viewer for browsing Dragon’s Dogma Online animations and inspecting how they retarget to Dragon’s Dogma 2. Compare both rigs side by side, browse by vocation, equip weapons and armor, and preview supported effects and targets. Both games start with stock character models and have independent male and female options.

## Install

You need Windows x64, the [.NET 10 SDK](https://dotnet.microsoft.com/download/dotnet/10.0), the [.NET 8 Desktop Runtime](https://dotnet.microsoft.com/download/dotnet/8.0), [WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/), [Python 3.13 x64](https://www.python.org/downloads/), and local DDON and DD2 game files. The DDON client must have `nativePC\rom` and use supported ARCC v7 archives. For **Export selected clip for DD2** and native DD2 motion browsing, also install Python 3.15 x64 and supply its executable with `-NativePython`.

Download and extract the release source ZIP from GitHub. Open PowerShell in the extracted folder. Replace the three example paths with your own.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Setup.ps1 -Ddon 'C:\Games\DDON' -Dd2 'C:\Games\Dragons Dogma 2' -NativePython 'C:\Python315\python.exe'
```

Setup fetches the external tools, creates local Python environments, and builds the desktop app. Open **DDON Reborn.exe** under `work\desktop`. First launch may take a few minutes to index DDON archives. Subsequent launches reuse the index. The app stores extracted files and your viewer outfit under `work`. Game files are read locally. The download contains the viewer source and build files. Bring your own game installation.

For browser use, add `-BrowserOnly` to setup, then run the following and open `http://127.0.0.1:8765`.

```powershell
.\.venv\Scripts\python.exe viewer_library.py --ddon 'C:\Games\DDON' --dd2 'C:\Games\Dragons Dogma 2' --skilltool .\work\skilltool\skilltool.exe --revil .\.local\revil-2.10.201\revil_toolset.exe --python315 .\.local\native-python\Scripts\python.exe
```

Omit `-NativePython` during setup and `--python315` at browser launch if you only need DDON previews and side-by-side retargeting.

## Test an animation in Dragon’s Dogma 2

1. Choose a DDON player animation and select the clip you want in the clip dropdown.
2. Click **Export selected clip for DD2**. The ZIP contains one PAK, one Lua script, and installation and removal instructions. Create these packages locally from your own game files.
3. Install [REFramework for DD2](https://www.nexusmods.com/dragonsdogma2/mods/8?tab=files) with custom PAK support and [_NickCore](https://www.nexusmods.com/dragonsdogma2/mods/1311). Follow their listed prerequisites.
4. With DD2 closed, extract the ZIP’s `pak_mods` and `reframework` folders into the DD2 game folder. A new export replaces the previous Motion Library test package. Start DD2, load a save, and stand idle on the Arisen.
5. Press **Insert**, open **DDON Reborn - Motion Library beta**, click **Play exported clip in 3 seconds**, and close the overlay. Watch the Arisen and check the panel’s playback status.
6. Close DD2 and remove `pak_mods/DDON_Reborn_Library_Preview.pak` and `reframework/autorun/ddon_reborn_library_preview.lua` to uninstall the test.

The exporter currently targets the standard male DD2 player rig. It exports one selected DDON player clip at a time and supports clips up to 30 seconds. The DD2 overlay plays the clip manually. Native skill slots, combat effects, and hit logic are outside this beta’s playback package.

## Report an issue

Include the game versions, vocation, archive, member, clip name, body selections, and what happened. Texture rendering, names, effects and skeleton mapping are still being validated. In-game material and effect appearance can differ from the preview.

## Dependencies and credits

Setup uses the [Arrowgene DDON client library](https://github.com/markup-ux/casual-dogma/tree/99e6aa391d3eddc24b592358a5133d3e923b39ee/Server/Arrowgene.Ddon.Client) from the pinned Casual Dogma snapshot (server source AGPL-3.0), [Revil Toolset](https://github.com/PredatorCZ/RevilLib/releases/tag/nightly) (GPL-3.0), and [REasy v0.8.0](https://github.com/seifhassine/REasy/tree/v0.8.0) (MIT). Setup downloads these from their publishers. The local DD2 PAK reader uses format references from [ree-pak-rs](https://github.com/eigeen/ree-pak-rs) and [dd2-mcp](https://github.com/holypolarpanda7/dd2-mcp). Dragon’s Dogma Online and Dragon’s Dogma 2 are Capcom games. This is a community tool.
