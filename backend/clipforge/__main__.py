import sys

# Windows consoles default to a legacy codepage (cp1252) that can't encode
# characters like "→" or curly quotes used in log/CLI messages; force UTF-8
# so those never crash the process instead of being printed.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from .cli import main

sys.exit(main())
