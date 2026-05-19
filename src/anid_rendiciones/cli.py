"""Entry point para `anid-rendiciones`. Lanza Streamlit en localhost."""

from __future__ import annotations

import sys
from pathlib import Path

from streamlit.web import cli as stcli


def main() -> None:
    app_path = Path(__file__).parent / "app.py"
    sys.argv = ["streamlit", "run", str(app_path), "--server.headless=false"]
    sys.exit(stcli.main())


if __name__ == "__main__":
    main()
