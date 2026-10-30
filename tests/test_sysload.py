"""W202 (T1464): CPU / process snapshot parsers (pure, no subprocess)."""
from webscraper import sysload


def test_parse_typeperf():
    out = ('\r\n"(PDH-CSV 4.0)","\\DELL\Processor(_Total)\% Processor Time"\r\n'
           '"10/30/2026 12:00:01.123","97.312500"\r\nExiting, please wait...\r\n')
    assert sysload.parse_typeperf(out) == 97.3
    assert sysload.parse_typeperf("garbage") is None


def test_parse_win_snapshot_scales_per_core_and_strips_instance_suffix():
    text = "\r\n".join([
        "TOTAL\t99",
        "P\t0\tIdle\t0\t8192",
        "P\t1234\tchrome#3\t200\t524288000",
        "P\t555\tMsMpEng\t40\t200000000",
        "P\tbad\tx\t1\t1",
        "C\t1234\t900\t\"C:\chrome.exe\" --user-data-dir=D:\ws\data\wa-profiles\acc1",
        "C\t900\t10\tpython -m webscraper agent --crm",
    ])
    s = sysload.parse_win_snapshot(text, cores=4)
    assert s["cpu_pct"] == 99.0
    assert 0 not in s["procs"]
    assert s["procs"][1234] == {"name": "chrome", "pid": 1234, "cpu": 50.0, "rss_mb": 524.3}
    assert s["procs"][555]["cpu"] == 10.0
    assert s["cmd"][1234][0] == 900
    ours = sysload.classify(s, me=900, data_dir=r"D:\ws\data")
    assert 1234 in ours and 900 in ours and 555 not in ours


def test_parse_ps_sums_cpu_over_cores():
    text = (" 10  1  150.0 204800 /usr/bin/python3 -m webscraper agent --crm\n"
            " 11 10   50.0 102400 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --user-data-dir=/x/data/p\n"
            " 12  1   10.0  10240 /usr/sbin/mds\n")
    s = sysload.parse_ps(text, cores=2)
    assert s["cpu_pct"] == 100.0                  # capped
    assert s["procs"][10]["cpu"] == 75.0
    assert s["procs"][12]["name"] == "mds"
    assert sysload.classify(s, me=10, data_dir="/x/data") >= {10, 11}


def test_by_lane_attributes_chrome_trees_by_profile():
    profiles = sorted([("/r/data/wa-profiles", "whatsapp"), ("/r/data/browser-profile-open", "maps"),
                       ("/r/data/browser-profile", "maps"), ("/r/data/fetch-profile", "websites")],
                      key=lambda t: -len(t[0]))
    text = ("100   1  40.0 1000 /usr/bin/python3 -m webscraper agent --crm\n"
            "200 100  80.0 1000 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --user-data-dir=/r/data/wa-profiles/acc1\n"
            "201 200  60.0 1000 /Applications/Google Chrome.app/Contents/Frameworks/Helper (Renderer) --type=renderer\n"
            "300 100  20.0 1000 /x/Chromium --user-data-dir=/r/data/browser-profile-open\n"
            "400 100  10.0 1000 /x/Chromium --user-data-dir=/r/data/fetch-profile\n"
            "500 100   4.0 1000 /x/node playwright driver\n"
            "600   1  30.0 1000 /usr/sbin/mds\n")
    s = sysload.parse_ps(text, cores=2)
    lanes = sysload.by_lane(s, 100, "/r/data", profiles, total=None)
    assert lanes == {"whatsapp": 70.0, "maps": 10.0, "websites": 5.0, "agent": 22.0, "other": 15.0}
    lanes = sysload.by_lane(s, 100, "/r/data", profiles, total=100.0)
    assert lanes["other"] == 0.0
