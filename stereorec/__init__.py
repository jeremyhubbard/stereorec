"""StereoRec: a self-contained, high-reliability stereo video recorder for the
Raspberry Pi 4 with an Arducam synchronized dual-camera (side-by-side) system.

Design priority (most important first):
1. Never lose completed recorded footage.
2. Detect and recover from failures automatically.
3. Continue recording with minimal interruption.
4. Maintain accurate session metadata.
5. Optimize performance only after reliability requirements are met.
"""

__version__ = "1.0.0"
