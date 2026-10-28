# thorlabs_tsi_sdk (vendored, BeamBox)

ThorLabs TSI camera SDK Python wrapper, version 0.0.8 (`version.py`), used by
`camera_thorlabs.py` for the Zelux camera.

- **Source:** copied 2026-10-28 from
  `C:\Users\MITBeamBox_01\AppData\Local\Programs\Python\Python313\Lib\site-packages\thorlabs_tsi_sdk`
  (`*.py` only). That copy had been hand-edited on this PC to load its DLLs from
  the old `Desktop\SOFTWARE\rastering\dlls` folder and
  `C:\Program Files\Thorlabs\ThorImageCAM\Bin`. It is left as it is: the old
  rastering GUI still uses it.
- **Change here (only one):** `tl_camera.py`, `TLCameraSDK.__init__` loads
  `thorlabs_tsi_camera_sdk.dll` by full path from `BEAMBOX_DLL_DIR` = this
  repo's `dlls\` (resolved relative to `tl_camera.py`, no user path), and puts
  `dlls\` first on `PATH`: the SDK loads its other modules by bare name with
  `LoadLibraryA`, which searches `PATH`, not `add_dll_directory` folders.
  `C:\Program Files\Thorlabs\ThorImageCAM\Bin` (SDK 2.2.5; repo `dlls\` is
  2.1.0) is on the system `PATH` after ours, so a module missing from `dlls\`
  (e.g. `thorlabs_tsi_logger.dll`) still comes from there. At first camera
  launch, check which DLL paths were loaded (BBX-173 review r2, R2-1).
- **Launch:** `python main_rastering.py` from the repo folder. Do not use
  `-P`, `-I` or `PYTHONSAFEPATH`: then the old site-packages copy is imported.
- **Why this copy wins over site-packages:** the package sits at the repo root.
  `python main_rastering.py` puts the script's folder first on `sys.path`, and
  the tests insert the repo root first. Check:
  `python -c "import thorlabs_tsi_sdk.tl_camera as t; print(t.__file__, t.BEAMBOX_DLL_DIR)"`
  run from the repo root (imports only; opens no camera).
