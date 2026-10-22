# Third-Party Software Licenses

This project uses several third-party software components. Their licenses are listed below.

## Python Dependencies

### [rsa](https://github.com/sybrenstuvel/python-rsa)
- **License:** Apache License 2.0
- **Copyright:** Copyright (c) 2011 Sybren A. Stüvel

### [pyasn1](https://github.com/pyasn1/pyasn1) (dependency of `rsa`)
- **License:** BSD 2-Clause License
- **Copyright:** Copyright (c) 2005-2024, Ilya Etingof <etingof@gmail.com>

### [psutil](https://github.com/giampaolo/psutil)
- **License:** BSD 3-Clause License
- **Copyright:** Copyright (c) 2009, Giampaolo Rodola

## Protocol References

### [androidtvremote2](https://github.com/tronikos/androidtvremote2)
- **License:** Apache License 2.0
- **Copyright:** Copyright (c) tronikos
- **Note:** The Android TV Remote protocol v2 message layouts (`polo.proto`, `remotemessage.proto`) follow this project. No code from it is bundled; the messages are encoded by hand in `core/remote_v2.py`.

---

For the full text of these licenses, please refer to their respective project repositories.
