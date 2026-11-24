"""Thorlabs KCube DC servo (one Z912 stage axis) through Kinesis .NET and pythonnet.

Only the motor thread calls a KCube (stage.py). pythonnet and the Kinesis DLLs load when the
first KCube is made, so importing this module loads nothing.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable, Optional

from config import HardwareConfig

PROGRESS_S = 0.2   # while a move or a home runs, its position is read this often for the display [s]

_DLLS = (
    "Thorlabs.MotionControl.DeviceManagerCLI.dll",
    "Thorlabs.MotionControl.GenericMotorCLI.dll",
    "Thorlabs.MotionControl.KCube.DCServoCLI.dll",
)
_kinesis_loaded = False


def _load_kinesis(kinesis_dir: str) -> None:
    """Load the Kinesis assemblies and build the device list, once per process."""
    global _kinesis_loaded
    if _kinesis_loaded:
        return
    import clr  # pythonnet

    for name in _DLLS:
        path = os.path.join(kinesis_dir, name)
        if not os.path.exists(path):
            raise RuntimeError(f"Kinesis DLL not found: {path} (set HardwareConfig.kinesis_dir)")
        clr.AddReference(path)
    from Thorlabs.MotionControl.DeviceManagerCLI import DeviceManagerCLI

    DeviceManagerCLI.BuildDeviceList()
    _kinesis_loaded = True


class TaskWaiter:
    """Waits for one Kinesis task (a move, a home) by its id. Kinesis calls `done(task_id)` from its
    own thread when a task ends; a late call for an older task does not end the wait."""

    def __init__(self):
        self._event = threading.Event()
        self._done_ids = set()

    def done(self, task_id) -> None:
        self._done_ids.add(int(task_id))
        self._event.set()

    def wait(self, task_id: int, timeout_s: float, *, every_s: Optional[float] = None,
             on_tick: Optional[Callable[[], None]] = None) -> bool:
        """True when task `task_id` ended within `timeout_s`. on_tick runs every `every_s` while
        it waits (to read the position on the way)."""
        deadline = time.monotonic() + timeout_s
        while task_id not in self._done_ids:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            if not self._event.wait(left if every_s is None else min(left, every_s)):
                if on_tick is not None:
                    on_tick()
                continue
            self._event.clear()
        self._done_ids.discard(task_id)
        return True


class KCube:
    """One KCube DC servo with a Z912 stage: positions [mm], calls block until the stage is done."""

    def __init__(self, serial_no: str, name: str, hw: HardwareConfig):
        _load_kinesis(hw.kinesis_dir)
        from System import Action, Decimal, UInt64
        from Thorlabs.MotionControl.DeviceManagerCLI import DeviceManagerCLI
        from Thorlabs.MotionControl.KCube.DCServoCLI import KCubeDCServo

        self.name, self.serial_no, self._hw = name, str(serial_no), hw
        self._Decimal = Decimal
        self._waiter = TaskWaiter()
        self._on_done = Action[UInt64](self._waiter.done)   # kept: Kinesis holds only a .NET reference
        self._device = None
        found = [str(d) for d in DeviceManagerCLI.GetDeviceList()]
        if self.serial_no not in found:
            raise RuntimeError(f"KCube {name} ({self.serial_no}) not found; connected: {found}")
        try:
            self._device = KCubeDCServo.CreateKCubeDCServo(self.serial_no)
            self._device.Connect(self.serial_no)
            time.sleep(0.25)
            self._device.StartPolling(int(hw.poll_ms))
            time.sleep(0.25)
            self._device.EnableDevice()
            time.sleep(0.25)
            if not self._device.IsSettingsInitialized():
                self._device.WaitForSettingsInitialized(int(hw.settings_wait_ms))
            if not self._device.IsSettingsInitialized():
                raise RuntimeError("device settings did not initialise")
            motor_config = self._device.LoadMotorConfiguration(self.serial_no)
            motor_config.DeviceSettingsName = str(hw.device_settings_name)
            motor_config.UpdateCurrentConfiguration()
            self._device.SetSettings(self._device.MotorDeviceSettings, True, False)
        except Exception as e:
            try:
                self.disconnect()
            except Exception:       # the start-up error below is the one the operator needs
                pass
            raise RuntimeError(f"KCube {name} ({self.serial_no}) did not start: {e}") from e

    def get_position(self) -> float:
        return float(self._Decimal.ToSingle(self._device.Position))

    def move_to(self, pos: float, on_progress: Optional[Callable[[float], None]] = None) -> float:
        """Move and return when there; on_progress(mm) gets the position on the way."""
        if abs(self.get_position() - float(pos)) < 1e-4:   # already there (0.1 um)
            return self.get_position()
        self._run(lambda done: self._device.MoveTo(self._Decimal(float(pos)), done),
                  self._hw.move_timeout_s, f"the move to {float(pos):.4f} mm", on_progress)
        return self.get_position()

    def home(self, on_progress: Optional[Callable[[float], None]] = None) -> float:
        self._run(lambda done: self._device.Home(done), self._hw.home_timeout_s, "homing", on_progress)
        return self.get_position()

    def set_backlash(self, value: float) -> float:
        """Set the backlash [mm]; returns what the controller took (it may clip)."""
        self._device.SetBacklash(self._Decimal(float(value)))
        return self.get_backlash()

    def get_backlash(self) -> float:
        return float(self._Decimal.ToSingle(self._device.GetBacklash()))

    def disconnect(self) -> None:
        if self._device is None:
            return
        device, self._device = self._device, None
        try:
            device.StopPolling()
        finally:
            device.Disconnect()

    def _run(self, start, timeout_s: float, what: str, on_progress=None) -> None:
        """Start a Kinesis task and wait for its completion callback; stop the stage on timeout.
        on_progress(mm): the position, read every PROGRESS_S while the task runs."""
        def tick():
            try:
                on_progress(self.get_position())
            except Exception:       # a missed reading only delays the display
                pass

        task_id = int(start(self._on_done))
        if not self._waiter.wait(task_id, timeout_s, every_s=PROGRESS_S,
                                 on_tick=tick if on_progress is not None else None):
            try:
                self._device.StopImmediate()
            except Exception:
                pass
            raise TimeoutError(f"motor {self.name} did not finish {what} within {timeout_s:g} s")
