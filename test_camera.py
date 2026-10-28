"""Manual Zelux check: opens the camera, grabs one raw frame, prints its
dtype/range/bit depth, closes. Run by hand only: `python test_camera.py`.
BeamBox: the body is under __main__ so importing this file (a bare `pytest`
from the repo root collects it) never opens the camera."""

if __name__ == "__main__":
    from camera_thorlabs import ThorCamera, ThorCamConfig
    import numpy as np

    cfg = ThorCamConfig(exposure_us=200000.0)  # 200ms
    cam = ThorCamera(cfg)
    cam.open()

    # Get raw frame before any bit shifting
    from thorlabs_tsi_sdk.tl_camera import TLCameraSDK
    cam._cam.issue_software_trigger()
    import time
    time.sleep(0.5)
    frame_data = cam._cam.get_pending_frame_or_null()
    raw = np.copy(frame_data.image_buffer)

    print("Raw dtype:", raw.dtype)
    print("Raw min:", raw.min(), "Raw max:", raw.max())
    print("Camera bit depth:", cam._cam.bit_depth)

    cam.close()
