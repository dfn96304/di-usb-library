from __future__ import annotations

import time
from infinity import InfinityBase, discover_bases

print("Found bases:", discover_bases())

base = InfinityBase(debug=True).connect()

base.onTagsChanged = lambda: print("Tags added or removed.")

print("All tags:", base.get_all_tags())

base.set_color(1, 200, 0, 0)
base.set_color(2, 0, 56, 0)
base.fade_color(3, 0, 0, 200)

time.sleep(3)
base.flash_color(3, 0, 0, 200)

print("Try adding/removing figures/discs to/from the base. CTRL-C to quit")
try:
    while True:
        time.sleep(1)
finally:
    base.disconnect()
