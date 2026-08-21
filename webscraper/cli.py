"""CLI: python -m webscraper <command>."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from webscraper import __version__
from webscraper.config import settings

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Slow, polite Google Maps lead scraper + website contact enricher.")
console = Console()
logging.basicConfig(level=logging.INFO, format="%(message)s",
                    handlers=[RichHandler(console=console, show_path=False)])
logging.getLogger("httpx").setLevel(logging.WARNING)


def _store():
    from webscraper.store import Store
    return Store()


@app.command()
def scrape(
    query: str = typer.Argument(..., help='Keyword, e.g. "dentist"'),
    location: Optional[str] = typer.Option(None, "--location", "-l", help='e.g. "Pune" or "Koregaon Park, Pune"'),
    max_places: int = typer.Option(100, "--max", "-n", help="Max places to collect (0 = unlimited)"),
    delay: Optional[float] = typer.Option(None, "--delay", "-d", help=f"Seconds between place visits (default {settings.delay_sec})"),
    headless: Optional[bool] = typer.Option(None, "--headless/--no-headless", help="Hide/show the browser"),
    country: Optional[str] = typer.Option(None, "--country", help=f"Phone region, default {settings.default_country}"),
    radius: Optional[float] = typer.Option(None, "--radius", "-r", help="Keep only places within this many km of LOCATION"),
) -> int:
    """Scrape Google Maps for QUERY (optionally in LOCATION). Creates a job; prints job id."""
    from webscraper.maps import Pacing, run_scrape

    if radius and not location:
        console.print("[red]--radius needs --location[/]")
        raise typer.Exit(2)
    store = _store()
    pacing = Pacing(delay_sec=delay or settings.delay_sec)
    job_id = store.create_job(query, location, max_places, pacing.delay_sec, radius_km=radius)
    console.print(f"[bold]job #{job_id}[/] — {query!r} in {location or 'anywhere'} · max {max_places} · {pacing.delay_sec:.0f}s/place")

    with Progress(TextColumn("[progress.description]{task.description}"), BarColumn(),
                  TextColumn("{task.completed}/{task.total}"), TimeElapsedColumn(), console=console) as prog:
        t_links = prog.add_task("collecting links", total=max_places or None)
        t_places = None

        def on_event(kind: str, data: dict) -> None:
            nonlocal t_places
            if kind == "links":
                prog.update(t_links, completed=min(data["count"], max_places) if max_places else data["count"])
            elif kind == "links_done":
                prog.update(t_links, completed=data["count"], total=max(data["count"], 1))
                t_places = prog.add_task("scraping places", total=data["count"])
            elif kind == "place" and t_places is not None:
                p = data["place"]
                prog.update(t_places, advance=1)
                console.log(f"{data['i']:>3}. {p.name or '?'} · {p.phone or '-'} · {p.website or '-'}")
            elif kind == "far":
                console.log(f"[dim]outside radius ({data['distance_km']} km): {data['name']}[/]")
            elif kind == "center":
                console.log(f"centre {data['lat']:.4f},{data['lng']:.4f} zoom {data['zoom']:.1f}")
            elif kind == "captcha":
                console.log(f"[red]captcha — backing off {data['backoff_sec']:.0f}s[/]")
            elif kind == "skip":
                console.log(f"[yellow]skip ({data['reason']})[/] {data['href'][:80]}")
            elif kind == "abort":
                console.log(f"[red]aborted: {data['reason']}[/]")

        try:
            n = run_scrape(store, job_id, query, location, max_places, pacing,
                           headless=headless, country=country, on_event=on_event, radius_km=radius)
        except KeyboardInterrupt:
            store.finish_job(job_id, "stopped", "interrupted")
            console.print("[yellow]stopped — progress saved; re-run enrich/export on this job[/]")
            raise typer.Exit(130)
    console.print(f"[green]saved {n} places[/] -> job #{job_id}. Next: [bold]python -m webscraper enrich --job {job_id}[/]")
    return job_id


@app.command()
def enrich(
    job: Optional[int] = typer.Option(None, "--job", "-j", help="Job id (default: all pending)"),
    concurrency: Optional[int] = typer.Option(None, "--concurrency", "-c", help=f"Parallel sites (default {settings.enrich_concurrency})"),
    country: Optional[str] = typer.Option(None, "--country"),
    redo: bool = typer.Option(False, "--redo", help="Re-enrich already-done rows too"),
) -> None:
    """Crawl each place's website for emails, socials and WhatsApp."""
    from webscraper.enrich import enrich_places

    store = _store()
    rows = store.places(job, None if redo else "pending")
    if not rows:
        console.print("nothing to enrich")
        return
    console.print(f"enriching {len(rows)} places (concurrency {concurrency or settings.enrich_concurrency})")
    with Progress(TextColumn("[progress.description]{task.description}"), BarColumn(),
                  TextColumn("{task.completed}/{task.total}"), TimeElapsedColumn(), console=console) as prog:
        t = prog.add_task("enriching", total=len(rows))

        def on_progress(r: dict, status: str) -> None:
            prog.update(t, advance=1)
            colour = {"done": "green", "no_website": "dim", "failed": "red", "thin": "yellow"}.get(status, "")
            console.log(f"[{colour}]{status:<10}[/] {r.get('name') or '?'} · {r.get('website') or '-'}")

        counts = asyncio.run(enrich_places(store, rows, concurrency, country, on_progress))
    console.print(f"[green]done[/] {counts}")


@app.command()
def export(
    job: Optional[int] = typer.Option(None, "--job", "-j", help="Job id (default: everything)"),
    fmt: str = typer.Option("csv", "--format", "-f", help="csv | json"),
    out: Optional[Path] = typer.Option(None, "--out", "-o"),
) -> None:
    """Write leads to data/exports/ (or --out)."""
    store = _store()
    path = store.export(job, fmt, out)
    console.print(f"[green]wrote[/] {path}")


@app.command()
def run(
    query: str = typer.Argument(...),
    location: Optional[str] = typer.Option(None, "--location", "-l"),
    max_places: int = typer.Option(100, "--max", "-n"),
    delay: Optional[float] = typer.Option(None, "--delay", "-d"),
    headless: Optional[bool] = typer.Option(None, "--headless/--no-headless"),
    fmt: str = typer.Option("csv", "--format", "-f"),
) -> None:
    """scrape -> enrich -> export in one go."""
    job_id = scrape(query, location, max_places, delay, headless, None, None)
    enrich(job_id, None, None, False)
    export(job_id, fmt, None)


@app.command()
def jobs() -> None:
    """List jobs."""
    store = _store()
    t = Table("id", "query", "location", "max", "found", "status", "created", "finished")
    for j in store.list_jobs():
        t.add_row(str(j["id"]), j["query"], j["location"] or "-", str(j["max_places"]), str(j["places"]),
                  j["status"], (j["created_at"] or "")[:16], (j["finished_at"] or "")[:16])
    console.print(t)


@app.command()
def stats() -> None:
    """Coverage numbers across everything scraped so far."""
    store = _store()
    s = store.stats()
    t = Table("metric", "count", "%")
    total = s["places"] or 1
    for k, v in s.items():
        pct = f"{100 * v / total:.0f}%" if k not in ("jobs", "places") else ""
        t.add_row(k, str(v), pct)
    console.print(t)


@app.command()
def serve(
    port: int = typer.Option(8765, "--port", "-p"),
    host: str = typer.Option("127.0.0.1", "--host"),
    no_browser: bool = typer.Option(False, "--no-browser", help="Don't auto-open the page"),
) -> None:
    """Start the local web UI (form -> live progress -> leads table -> Excel download)."""
    from webscraper.server import serve as _serve

    console.print(f"[bold]web UI[/] http://{host}:{port}/  (Ctrl-C to stop)")
    _serve(host=host, port=port, open_browser=not no_browser)


@app.command()
def agent(
    token: str = typer.Option("", "--token", help="Agent token; falls back to CRM_AGENT_TOKEN in .env"),
    base: str = typer.Option("", "--base", help="Override the API base URL"),
    crm: bool = typer.Option(False, "--crm", help="Serve the HVT CRM Lead Finder queue instead of the SaaS cloud"),
    poll: int = typer.Option(20, "--poll", help="Seconds between cloud polls"),
) -> None:
    """Run cloud jobs on this machine: poll -> scrape locally -> sync results up.

    With no --token, reads CRM_AGENT_TOKEN (CRM mode) / AGENT_TOKEN (SaaS) from .env, so
    `run-agent` scripts can start it hands-free once the token is saved once.
    """
    import os

    from webscraper.agent import run_agent

    token = token or os.getenv("CRM_AGENT_TOKEN" if crm else "AGENT_TOKEN") or os.getenv("AGENT_TOKEN") or ""
    if not token:
        console.print("[red]no token[/] — pass --token or set CRM_AGENT_TOKEN in .env "
                      "(mint one on the CRM Lead Finder → Setup tab)")
        raise typer.Exit(1)
    # CRM base, in priority order: --base, CRM_FUNCTIONS_URL, derived from the tenant's
    # own VITE_SUPABASE_URL (so a CLONED CRM's agent targets ITS project, not HVT's),
    # then the HVT default. This is what makes `run-agent` work unchanged inside any
    # cloned tenant folder — its .env carries VITE_SUPABASE_URL + CRM_AGENT_TOKEN.
    def _crm_base() -> str:
        env = os.getenv("CRM_FUNCTIONS_URL")
        if env:
            return env.rstrip("/")
        supa = os.getenv("VITE_SUPABASE_URL")
        if supa:
            return supa.rstrip("/") + "/functions/v1"
        return "https://fyfhkjxewzbyxdwspkuc.supabase.co/functions/v1"
    resolved = base or (_crm_base() if crm else "https://web-scraper-leads.vercel.app")
    console.print(f"[bold]agent[/] ({'crm' if crm else 'saas'}) polling {resolved} every {poll}s  (Ctrl-C to stop)")
    run_agent(resolved, token, poll, kind="crm" if crm else "saas")


@app.command(name="wa-login")
def wa_login(name: str = typer.Argument(..., help="Account label, e.g. 'spare1'")) -> None:
    """Link a WhatsApp account for number verification: opens a window, scan the QR once."""
    from webscraper import wa_verify
    console.print(f"[bold]Opening WhatsApp Web for [cyan]{name}[/][/] — scan the QR with that phone.")
    ok = wa_verify.login(name)
    console.print("[green]linked[/]" if ok else "[red]not linked (timed out)[/]")


@app.command(name="wa-accounts")
def wa_accounts() -> None:
    """List linked WhatsApp accounts and today's usage against the daily cap."""
    from webscraper.config import settings
    from webscraper.store import Store
    rows = Store().list_wa_accounts()
    if not rows:
        console.print("no accounts — run [cyan]python -m webscraper wa-login <name>[/]")
        return
    cap = settings.wa_daily_cap
    active = sum(0 if a["disabled"] else 1 for a in rows)
    for a in rows:
        state = "disabled" if a["disabled"] else "active"
        console.print(f"[bold]{a['name']}[/]  {a['sent_today']}/{cap} today  · {state}"
                      f" · last used {a['last_used_at'] or 'never'}")
    console.print(f"[dim]total capacity/day ~ {cap * active} across {active} active account(s)[/]")


@app.command(name="wa-verify")
def wa_verify_cmd(job_id: int = typer.Argument(..., help="Verify the numbers of this job's leads")) -> None:
    """Run WhatsApp verification over an existing job's leads (rotates accounts, respects the cap)."""
    from webscraper import wa_verify
    from webscraper.store import Store
    s = Store()
    rows = [p for p in s.places(job_id) if (p.get("phone") or p.get("whatsapp_number"))]
    console.print(f"verifying {len(rows)} numbers for job {job_id}…")
    res = wa_verify.verify_places(s, rows, job_id=job_id)
    console.print(res)


@app.command()
def version() -> None:
    console.print(__version__)
