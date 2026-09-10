"""T562 — does each Valve World exhibitor's number exist on WhatsApp? → new Excel columns.

    python scripts/vwe-wa-check.py [--json data/exports/valveworld2026-exhibitors.json]
                                   [--accounts hvt_wa_bus_1,hvt_wa_bus_2] [--limit N] [--resume]

One number per exhibitor (the published wa.me number if the site had one, else the expo
profile phone, else the first website phone), checked on WhatsApp Web with the same machinery
the Lead Finder lane uses (`wa_verify.verify_places`, account rotation + pacing + the daily
cap). Verdicts are appended to a JSONL checkpoint after EVERY number, so a crash or a stop
loses nothing and `--resume` (default) skips what is already decided.

Writes back into the workbook: `whatsapp_exists` (yes / no / unknown / no number),
`whatsapp_checked_number`, `whatsapp_checked_at`.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from webscraper import wa_verify  # noqa: E402
from webscraper.store import Store, plus  # noqa: E402

log = logging.getLogger("vwe-wa")
DIGITS = re.compile(r"\D+")


def e164(raw: str | None, cc: str | None = None) -> str | None:
    """'+49 5208 9102-0' → '+4952089102 0' → +E.164 digits. Leading 00 → +, bare national
    numbers are left alone (no country guess: a wrong +CC is a wrong verdict)."""
    if not raw:
        return None
    s = str(raw).strip()
    if s.startswith("00"):
        s = "+" + s[2:]
    d = DIGITS.sub("", s)
    if not d:
        return None
    if not s.startswith("+"):
        return None          # only fully-qualified numbers are checkable
    return plus(d) if 8 <= len(d) <= 15 else None


def pick_number(row: dict) -> tuple[str | None, str]:
    """The one number to check for this company, and where it came from."""
    for src, val in (("wa_link", row.get("whatsapp")), ("expo", row.get("phone"))):
        n = e164(val)
        if n:
            return n, src
    for p in (row.get("site_phones") or "").split(","):
        n = e164(p.strip())
        if n:
            return n, "site"
    return None, ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="data/exports/valveworld2026-exhibitors.json")
    ap.add_argument("--xlsx", default="")
    ap.add_argument("--accounts", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-resume", action="store_true")
    ap.add_argument("--headless", action="store_true", default=True)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    src = Path(args.json)
    rows = json.load(open(src, encoding="utf-8"))
    ckpt = src.with_suffix(".wa.jsonl")
    done: dict[str, dict] = {}
    if ckpt.exists() and not args.no_resume:
        for line in ckpt.open(encoding="utf-8"):
            try:
                r = json.loads(line)
                done[r["exh_id"]] = r
            except Exception:  # noqa: BLE001
                pass
        log.info("resuming — %d already checked", len(done))

    targets: list[dict] = []
    for r in rows:
        num, how = pick_number(r)
        r["whatsapp_checked_number"] = num or ""
        if not num:
            r["whatsapp_exists"] = "no number"
            continue
        if r["exh_id"] in done:
            continue
        targets.append({"place_key": r["exh_id"], "number": num, "source": how, "name": r["name"]})
    if args.limit:
        targets = targets[: args.limit]
    log.info("%d exhibitors · %d numbers to check (%d skipped: already done or no number)",
             len(rows), len(targets), len(rows) - len(targets))
    if not targets:
        write_back(rows, done, src, args.xlsx)
        return

    store = Store()
    accounts = [a.strip() for a in args.accounts.split(",") if a.strip()] or store.enabled_wa_accounts()
    if not accounts:
        log.error("no WhatsApp account is linked on this machine — run `python -m webscraper wa-login <name>`")
        sys.exit(2)
    log.info("accounts: %s · headless=%s", ", ".join(accounts), args.headless)

    lock = threading.Lock()
    fh = ckpt.open("a", encoding="utf-8")
    counts = {"yes": 0, "no": 0, "unknown": 0}
    t0 = time.time()

    def record(pk: str, status: str, num: str | None, source: str | None) -> None:
        rec = {"exh_id": pk, "verdict": status, "number": num, "source": source, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        with lock:
            done[pk] = rec
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            counts[status] = counts.get(status, 0) + 1
            n = sum(counts.values())
            if n % 10 == 0:
                rate = n / max(1e-9, (time.time() - t0) / 60)
                left = (len(targets) - n) / max(rate, 1e-9)
                log.info("%d/%d · yes %d no %d unknown %d · %.1f/min · ~%.0f min left",
                         n, len(targets), counts["yes"], counts["no"], counts["unknown"], rate, left)

    # One thread per account, its own Store (sqlite connections are not thread-safe) and its
    # own slice of the numbers — the same shape the WhatsApp lane uses.
    slices: list[list[dict]] = [targets[i::len(accounts)] for i in range(len(accounts))]
    threads = []
    for acct, part in zip(accounts, slices):
        if not part:
            continue

        def run(acct: str = acct, part: list[dict] = part) -> None:
            st = Store()
            try:
                wa_verify.verify_places(st, part, on_progress=record, job_id=None,
                                        headless=args.headless, account=acct)
            except Exception:  # noqa: BLE001
                log.exception("slice %s failed", acct)
        t = threading.Thread(target=run, name=f"wa-{acct}", daemon=False)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    fh.close()
    log.info("checked %d in %.0f min · yes %d · no %d · unknown %d",
             sum(counts.values()), (time.time() - t0) / 60, counts["yes"], counts["no"], counts["unknown"])
    write_back(rows, done, src, args.xlsx)


def write_back(rows: list[dict], done: dict[str, dict], src: Path, xlsx_arg: str) -> None:
    for r in rows:
        rec = done.get(r["exh_id"])
        if rec:
            r["whatsapp_exists"] = rec["verdict"]
            r["whatsapp_checked_number"] = rec.get("number") or r.get("whatsapp_checked_number", "")
            r["whatsapp_checked_at"] = rec.get("at", "")
        else:
            r.setdefault("whatsapp_exists", "not checked")
            r.setdefault("whatsapp_checked_at", "")
    json.dump(rows, open(src, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    from openpyxl import load_workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    xp = Path(xlsx_arg) if xlsx_arg else src.with_suffix(".xlsx")
    wb = load_workbook(xp)
    ws = wb["Exhibitors"]
    head = [c.value for c in ws[1]]
    new = ["whatsapp_exists", "whatsapp_checked_number", "whatsapp_checked_at"]
    col = {}
    for name in new:
        if name in head:
            col[name] = head.index(name) + 1
        else:
            ws.cell(row=1, column=ws.max_column + 1, value=name).font = Font(bold=True)
            col[name] = ws.max_column
    idx = {ws.cell(row=r, column=head.index("exh_id") + 1).value: r for r in range(2, ws.max_row + 1)}
    green = PatternFill("solid", fgColor="D6F5D6")
    grey = PatternFill("solid", fgColor="F0F0F0")
    for r in rows:
        rw = idx.get(r["exh_id"])
        if not rw:
            continue
        v = r.get("whatsapp_exists", "")
        ws.cell(row=rw, column=col["whatsapp_exists"], value=v)
        ws.cell(row=rw, column=col["whatsapp_checked_number"], value=r.get("whatsapp_checked_number", ""))
        ws.cell(row=rw, column=col["whatsapp_checked_at"], value=r.get("whatsapp_checked_at", ""))
        if v == "yes":
            ws.cell(row=rw, column=col["whatsapp_exists"]).fill = green
        elif v in ("no number", "not checked"):
            ws.cell(row=rw, column=col["whatsapp_exists"]).fill = grey
    for name in new:
        ws.column_dimensions[get_column_letter(col[name])].width = 22
    wb.save(xp)
    tally: dict[str, int] = {}
    for r in rows:
        tally[r.get("whatsapp_exists", "")] = tally.get(r.get("whatsapp_exists", ""), 0) + 1
    print("wrote", xp, "·", ", ".join(f"{k} {v}" for k, v in sorted(tally.items(), key=lambda kv: -kv[1])))


if __name__ == "__main__":
    main()
