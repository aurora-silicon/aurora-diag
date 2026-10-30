# SPDX-License-Identifier: MIT
"""Which machine this is, from the device tree's compatible strings.

Support is decided by the SoC family, never by a list of boxes: any board on
a known Apple SoC gets the control side, everything else is read-only unless
the user passes --allow-unknown. Features (fans, haptics, lid angle) are then
detected from what the kernel exposes, not from the board name.
"""

from . import sysfs

# Apple SoC compatibles whose kernel interfaces this tool knows.
KNOWN_SOCS = {
    "apple,t8103": "M1",
    "apple,t6000": "M1 Pro",
    "apple,t6001": "M1 Max",
    "apple,t6002": "M1 Ultra",
    "apple,t8112": "M2",
    "apple,t6020": "M2 Pro",
    "apple,t6021": "M2 Max",
    "apple,t6022": "M2 Ultra",
    "apple,t8122": "M3",
    "apple,t6030": "M3 Pro",
    "apple,t6031": "M3 Max",
    "apple,t6034": "M3 Max",
    "apple,t8132": "M4",
    "apple,t6040": "M4 Pro",
    "apple,t6041": "M4 Max",
    "apple,t8140": "A18 Pro",
}

# Features a board must not get even when its hardware looks like it has
# them. Keyed by board compatible. Kept short on purpose: everything else is
# detected.
FEATURE_EXCLUSIONS = {
    # MacBook Neo: the trackpad actuator path is out of scope for this tool.
    "apple,j700": {"haptics"},
}


class Machine:
    def __init__(self):
        self.compatible = sysfs.dt_strings("/proc/device-tree/compatible")
        self.model = (sysfs.read("/proc/device-tree/model") or "").rstrip("\0")
        self.board = self.compatible[0] if self.compatible else None
        self.soc = next((c for c in self.compatible if c in KNOWN_SOCS), None)
        self.apple = "apple,arm-platform" in self.compatible

    @property
    def chip(self):
        return KNOWN_SOCS.get(self.soc)

    @property
    def known(self):
        return self.apple and self.soc is not None

    def excluded(self, feature):
        return feature in FEATURE_EXCLUSIONS.get(self.board, set())

    def as_dict(self):
        return {
            "compatible": self.compatible,
            "model": self.model,
            "board": self.board,
            "soc": self.soc,
            "chip": self.chip,
            "known": self.known,
            "exclusions": sorted(FEATURE_EXCLUSIONS.get(self.board, set())),
        }
