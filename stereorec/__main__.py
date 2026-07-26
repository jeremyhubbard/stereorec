"""Entry point: python -m stereorec"""

from __future__ import annotations

import sys

from stereorec import logging_setup, sd_notify
from stereorec.app import RecorderApp
from stereorec.config import Config


def main() -> int:
    config = Config.load()
    logging_setup.init_logging(config)
    sd_notify.ready()
    app = RecorderApp(config)
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
