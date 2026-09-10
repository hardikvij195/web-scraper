"""W105 (CRM T553): the agent writes the open-files limit into the Mac launchd plist itself."""
from __future__ import annotations

from webscraper.fdcount import ensure_launchd_limit, LAUNCHD_LABEL

OLD_PLIST = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>/x/run-agent-loop.sh</string></array>
  <key>RunAtLoad</key><true/>
</dict></plist>
"""


def test_adds_the_key_once_and_keeps_the_rest(tmp_path):
    p = tmp_path / "agent.plist"
    p.write_text(OLD_PLIST, encoding="utf-8")
    assert ensure_launchd_limit(p) == "added"
    text = p.read_text(encoding="utf-8")
    assert "<key>SoftResourceLimits</key><dict><key>NumberOfFiles</key><integer>4096</integer></dict>" in text
    assert text.count("SoftResourceLimits") == 1
    assert text.endswith("</dict></plist>\n")
    assert "<key>RunAtLoad</key><true/>" in text
    assert ensure_launchd_limit(p) == "present"          # idempotent


def test_present_absent_and_garbage(tmp_path):
    p = tmp_path / "with.plist"
    p.write_text(OLD_PLIST.replace("</dict></plist>", "  <key>SoftResourceLimits</key><dict><key>NumberOfFiles</key><integer>4096</integer></dict>\n</dict></plist>"), encoding="utf-8")
    assert ensure_launchd_limit(p) == "present"
    assert ensure_launchd_limit(tmp_path / "missing.plist") == "absent"
    g = tmp_path / "garbage.plist"
    g.write_text("not a plist", encoding="utf-8")
    assert ensure_launchd_limit(g) == "error"
