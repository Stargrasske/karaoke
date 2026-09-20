"""Cross-platform audio input resolution for ffmpeg-based capture.

:mod:`karaoke.recorder` and :mod:`karaoke.sample_audio` both shell out to
ffmpeg to capture audio, but "what to capture" and "how to tell ffmpeg" are
platform-specific:

- **Linux** (the original, primary target): PipeWire/PulseAudio via ``pactl``.
  Sources are named strings (``<sink>.monitor`` for loopback of what is
  playing, or the default source for the microphone), fed to ffmpeg's
  ``-f pulse`` input.
- **Windows**: no PulseAudio. Devices are enumerated through ffmpeg's own
  ``-f dshow -list_devices`` and captured with ``-f dshow -i audio="<name>"``.
  There is no monitor/loopback equivalent exposed this way — DirectShow only
  sees input devices (microphones, line-in), not "what a sink is playing".
  Capturing system playback on Windows would need WASAPI loopback via a
  virtual/stereo-mix device, which is opt-in per machine and out of scope
  here; this module only wires up microphone-style input capture on Windows.

Song identification via ``songrec`` (see :mod:`karaoke.identify`) has no
Windows build at all — that remains Linux-only regardless of this module.
"""
from __future__ import annotations

import platform
import re
import shutil
import subprocess
from typing import Optional

IS_WINDOWS = platform.system() == "Windows"


def ffmpeg_input_args(source: str, *, windows_device: bool = False) -> list[str]:
    """ffmpeg ``-f ... -i ...`` args for the given source.

    ``windows_device=True`` means ``source`` is a DirectShow device name (as
    returned by :func:`list_windows_audio_devices`); otherwise it is treated as
    a PulseAudio/PipeWire source name (as returned by
    :mod:`karaoke.sample_audio`/``pactl``). Callers decide this explicitly
    rather than inferring it from the host OS alone, since a source resolved
    via the Linux monitor path is always a PulseAudio name even when the code
    happens to run under test on a Windows host.
    """
    if windows_device:
        return ["-f", "dshow", "-i", f"audio={source}"]
    return ["-f", "pulse", "-i", source]


def list_windows_audio_devices() -> list[str]:
    """Names of DirectShow audio-capture devices (microphones/line-in), via ffmpeg.

    Returns ``[]`` if ffmpeg is missing or none are found. Best-effort: ffmpeg
    reports the device list on stderr with a nonzero exit code by design (a
    ``dummy`` input never succeeds), so a failing return code alone does not
    mean no devices exist.
    """
    if not shutil.which("ffmpeg"):
        return []
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    devices: list[str] = []
    for line in proc.stderr.splitlines():
        # Each device is reported as `"<name>" (audio)` or `(video)`; the
        # alternative-name line right below it is not a device and is skipped.
        m = re.match(r'^.*"([^"]+)"\s*\(audio\)\s*$', line)
        if m:
            devices.append(m.group(1))
    return devices


def default_windows_microphone() -> Optional[str]:
    """First enumerated DirectShow audio device, or None if none are found."""
    devices = list_windows_audio_devices()
    return devices[0] if devices else None


def default_input_source() -> Optional[str]:
    """The best-effort default capture source name for this platform.

    Linux: delegates to the existing PulseAudio default-source resolution in
    :mod:`karaoke.identify`. Windows: the first DirectShow audio device.
    """
    if IS_WINDOWS:
        return default_windows_microphone()
    from .identify import _default_source
    return _default_source(mic=True)
