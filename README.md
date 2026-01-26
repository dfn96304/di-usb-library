di-usb-library patch, working with modern python/OS versions

## Dependencies
Python: `pip install hid`

If that fails on your platform: `pip install hidapi`

Linux dependencies (often needed): `sudo apt-get install libhidapi-hidraw0 libhidapi-libusb0 libhidapi-dev`

## Linux permissions (udev)
Create: /etc/udev/rules.d/99-disney-infinity.rules

`SUBSYSTEM=="hidraw", ATTRS{idVendor}=="0e6f", ATTRS{idProduct}=="0129", MODE="0666"`

Then:
- `sudo udevadm control --reload-rules`
- `sudo udevadm trigger`
- unplug/replug the base

## Run
    python3 test.py
