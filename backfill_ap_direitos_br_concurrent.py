#!/usr/bin/env python3
"""Compatibility wrapper.

The BR bulk scan now uses the batch-Aeolus implementation in
copyright_alert/backfill_ap_direitos_br.py as the permanent default.
"""

from copyright_alert.backfill_ap_direitos_br import main


if __name__ == "__main__":
    main()
