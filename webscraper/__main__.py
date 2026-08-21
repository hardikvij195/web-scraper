import sys

# Windows consoles default to cp1252; rich output (arrows, bullets) needs UTF-8.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

from webscraper.cli import app  # noqa: E402

app()
