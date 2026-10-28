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
  repo's `dlls\` (resolved relative to `tl_camera.py`, no user path).
  ThorImageCAM\Bin stays registered as a DLL directory, as before.
- **Why this copy wins over site-packages:** the package sits at the repo root.
  `python main_rastering.py` puts the script's folder first on `sys.path`, and
  the tests insert the repo root first. Check:
  `python -c "import thorlabs_tsi_sdk.tl_camera as t; print(t.__file__, t.BEAMBOX_DLL_DIR)"`
  run from the repo root (imports only; opens no camera).
