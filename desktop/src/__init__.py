"""viewhelper desktop application package.

Layout mirrors PRD section 24. Heavy optional dependencies (funasr, torch,
pyaudiowpatch, pynput, mss, sounddevice) are imported lazily inside functions so
that a missing module degrades gracefully instead of crashing the process
(PRD section 27).
"""

__version__ = "0.1.0"
