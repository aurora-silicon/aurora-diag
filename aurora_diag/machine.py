# SPDX-License-Identifier: MIT
"""Which machine this is, from the device tree alone.

There is no table of machines or SoCs here. The board and SoC compatibles,
the model string and the boot chain versions come straight from the device
tree; every feature (fans, haptics, lid angle, GPU) is then detected from
what the kernel exposes. A machine whose device tree does not say
"apple,arm-platform" is read-only unless the user passes --allow-unknown.
"""

import re

from . import sysfs

CHOSEN = "/proc/device-tree/chosen"
# Boot chain and firmware versions the loaders publish in /chosen.
CHOSEN_VERSIONS = re.compile(r"^(aurora,.*version|u-boot,version|asahi,.*version)$")
# LEGACY-ASAHI(2027-02-28): aura builds before the rename publish only the asahi, names
# (e.g. asahi,m1n1-stage2-version). They are reported under the new name
# (aurora,aura-stage2-version); a property published under the new name wins.
LEGACY_CHOSEN_PREFIX = "asahi,"


def chosen_name(prop):
    """The current name of a /chosen property (pre-rename names mapped to aurora,)."""
    if prop.startswith(LEGACY_CHOSEN_PREFIX):
        return "aurora," + prop[len(LEGACY_CHOSEN_PREFIX):].replace("m1n1", "aura")
    return prop



class Machine:
    def __init__(self):
        self.compatible = sysfs.dt_strings("/proc/device-tree/compatible")
        self.model = (sysfs.read("/proc/device-tree/model") or "").rstrip("\0")
        self.board = self.compatible[0] if self.compatible else None
        self.apple = "apple,arm-platform" in self.compatible
        # The SoC is the last vendor compatible before the platform string.
        socs = [c for c in self.compatible[1:] if c != "apple,arm-platform"]
        self.soc = socs[-1] if socs else None

    @property
    def known(self):
        """Changes are allowed on any Apple silicon device tree."""
        return self.apple

    def firmware(self):
        out = {}
        props = [p for p in sysfs.listdir(CHOSEN) if CHOSEN_VERSIONS.match(p)]
        # Native names last, so they replace a mapped legacy name.
        for p in sorted(props, key=lambda p: p.startswith(LEGACY_CHOSEN_PREFIX), reverse=True):
            out[chosen_name(p)] = sysfs.dt_value(CHOSEN + "/" + p)
        return out

    def as_dict(self):
        return {
            "compatible": self.compatible,
            "model": self.model,
            "board": self.board,
            "soc": self.soc,
            "apple": self.apple,
            "firmware": self.firmware(),
        }
