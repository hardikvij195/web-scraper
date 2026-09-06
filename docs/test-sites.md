# Lead Finder — website regression list (W56 / CRM T398)

Generated 2026-09-07 from every Lead Finder job run so far: **16,563 distinct websites** across jobs #2, #5, #6, #7, #11, #14, #15, #16, #17, #18, #20, #22, #23, #24, #25, #26, #27, #28, #29, #30, #31, #34, #35, #36, #40, #44, #45, #46, #47, #48, #49, #50, #51, #52, #1619, #1621. Full table: [`test-sites.csv`](./test-sites.csv). Run `python scripts/regress-sites.py` (all sections) or `--section <name>` after any scraper change; compare the totals with the previous `--json`.

Columns: **domain** · **why it is interesting** · **expect** (`reachable` = any tier reads it · `email` / `social` / `any` = that gets extracted · `dead` = must STAY unreadable) · **result at generation** (CRM outcome, and the 2026-09-07 re-probe from this PC where one ran).

## Snapshot

| Outcome | Distinct domains |
|---|---|
| clean | 8,180 |
| done_empty | 1,434 |
| thin | 377 |
| dns | 453 |
| connection | 330 |
| timeout | 133 |
| gone | 178 |
| http_403 | 25 |
| hard_block | 45 |
| cloudflare_wall | 16 |
| server_5xx | 58 |
| odd_4xx | 34 |
| recaptcha | 2 |
| rate_limited | 1 |

Tier that read the home page (job-log lines): **httpx** 14,190, **browser** 192, **tls** 177.

## Cloudflare challenge walls (cf_managed / cf_interactive / cf_non_interactive)

The browser tier classified the wall; the click-through (`ENRICH_CF_CLICK`) is what should clear managed/interactive. A non-interactive wall that never clears from a residential Chrome is the real test of the profile-cookie reuse.

| domain | why | expect | result at generation |
|---|---|---|---|
| `northwestgreetings.co.uk` | cf_non_interactive · GB · job 36 | reachable | still 403 [cloudflare,cf_challenge] |
| `cavendishnuclear.com` | cf_non_interactive · GB · job 36 | reachable | still 403 [cloudflare,cf_challenge] |
| `timesengineering.co.uk` | cf_non_interactive · GB · job 36 | reachable | still 403 [cloudflare,cf_challenge] |
| `depawater.co.uk` | cf_non_interactive · GB · job 36 | reachable | still 403 [cloudflare,cf_challenge] |
| `eastlondonofficecleaning.co.uk` | cf_managed · GB · job 45 | reachable | still 403 [cloudflare,cf_challenge] |
| `cateringhygiene.co.uk` | cf_managed · GB · job 45 | reachable | still 403 [cloudflare,cf_challenge] |
| `123cleaners.com` | cf_interactive · GB · job 45 | reachable | still 403 [cloudflare,cf_challenge] |
| `islingtonendoftenancy.uk` | cf_managed · GB · job 45 | reachable | still 403 [cloudflare,cf_challenge] |
| `cleannatural.co.uk` | cf_non_interactive · GB · job 45 | reachable | still 403 [cloudflare,cf_challenge] |
| `tlc-perth.com` | cf_managed · AU · job 48 | reachable | still 403 [cloudflare,cf_challenge] |

## Hard block pages (200/403 with a deny body, no challenge)

`blocked` = a body matching a deny marker with no Cloudflare class. Mix of Cloudflare 1020 (now `cf_deny`), Imperva/Sucuri pages and hosting 'suspended' pages. Expect the new `cf_deny` split to move most of these; the rest need a proxy or are truly gone.

| domain | why | expect | result at generation |
|---|---|---|---|
| `dtwtools.co.uk` | blocked · GB · job 36 | reachable | still 403 [cloudflare,cf_challenge] |
| `thesportscompanybelfast.com` | blocked · GB · job 36 | reachable | still 403 [cloudflare] |
| `houseoftiles.ie` | blocked · IE · job 36 | reachable | still 403 [cloudflare,cf_challenge] |
| `cannoncashandcarry.co.uk` | blocked · GB · job 44 | reachable | still 403 [cloudflare] |
| `aspris.co.uk` | blocked · GB · job 44 | reachable | still 403 [cloudflare,cf_challenge] |
| `magichand.co.uk` | blocked · GB · job 45 | reachable | still 403 [cloudflare,cf_challenge] |
| `cewgroup.co.uk` | blocked · GB · job 46 | reachable | still 403 [cloudflare,cf_challenge] |
| `directwoodflooring.co.uk` | blocked · GB · job 46 | reachable | still 403 [cloudflare] |
| `clevercarpets.co.uk` | blocked · GB · job 46 | reachable | still 403 [cloudflare] |
| `bathroomsatsource.com` | blocked · GB · job 46 | reachable | still 403 [cloudflare] |

## Plain HTTP 403

The fingerprint 403 — the case the TLS-impersonation tier exists for. With W56 the tier rotates chrome → safari → firefox before the browser.

| domain | why | expect | result at generation |
|---|---|---|---|
| `lookers.co.uk` | http_403 · GB · job 6 | reachable | still 403 [akamai] · browser blocked |
| `mayfairmotorsolutions.com` | http_403 · GB · job 6 | reachable | still 403 [cloudflare,cf_challenge] · browser blocked |
| `varianse.com` | http_403 · GB · job 7 | reachable | curl_cffi chrome ok (httpx 403) |
| `fullcarchecks.co.uk` | http_403 · GB · job 7 | reachable | still 403 [cloudflare] |
| `sixt.co.uk` | http_403 · GB · job 7 | reachable | curl_cffi chrome ok (httpx 403) |
| `st-johns-wood-autos.jany.io` | http_403 · GB · job 7 | reachable | still 403 [cloudflare,cf_challenge] |
| `truckandvanplus.co.uk` | http_403 · GB · job 7 | reachable | still 403 [cloudflare,cf_challenge] |
| `addlestonecommercials.co.uk` | http_403 · GB · job 7 | reachable | browser ok (httpx 403) |
| `tvcexports.com` | http_403 · GB · job 7 | reachable | curl_cffi chrome ok (httpx 403) |
| `global-commercials.com` | http_403 · GB · job 7 | reachable | curl_cffi chrome ok (httpx 403) |

## Rate limited (429) and odd 4xx (418 / 406 / 409 / 451 / 402)

418 = Wordfence/mod_security 'teapot' block, 406 = mod_security on the header set, 402 = expired hosting, 451 = geo-blocked. 429 now backs off once (Retry-After) before it is stored.

| domain | why | expect | result at generation |
|---|---|---|---|
| `ldcharteredaccountants.com` | http_401 · GB · job 16 | reachable | still 401 [squarespace,squarespace] |
| `schengen-visa-danismani.ueniweb.com` | http_402 · GB · job 16 | reachable | still 402 [cloudflare] |
| `moryarealtors.com` | http_400 · IN · job 30 | reachable | httpx 200 ok now |
| `dryfruitvala.com` | http_409 · IN · job 35 | reachable | still 403 [cloudflare] |
| `bhartihomeproducts.com` | http_409 · IN · job 35 | reachable | still network |
| `chemistreejeans.com` | http_409 · IN · job 35 | reachable | still 409 [cloudflare] |
| `candesworld.com` | http_402 · IN · job 35 | reachable | still 402 [cloudflare,shopify,expired] |
| `humgenterprises.com` | http_402 · IN · job 35 | reachable | httpx 200 ok now |

## Timeouts

A 15 s httpx timeout is often a host that stalls non-browsers (soft block) rather than a slow site. Browser tier is the test; a site that times out in Chrome too is simply down.

| domain | why | expect | result at generation |
|---|---|---|---|
| `autodetailinglondon.co.uk` | timeout · GB · job 7 | reachable | httpx 200 ok now |
| `angliaorthodontics.co.uk` | timeout · GB · job 11 | reachable | still timeout · browser network |
| `nursekatie.net` | timeout · GB · job 11 | reachable | httpx 200 ok now |
| `zedconsulting.co.uk` | timeout · GB · job 16 | reachable | httpx 200 ok now |
| `top-consultant.com` | timeout · GB · job 16 | reachable | still 503 |
| `plsgroupuk.com` | timeout · GB · job 16 | reachable | httpx 200 ok now |
| `portwaysolicitors.com` | timeout · GB · job 16 | reachable | httpx 200 ok now |
| `debitcredit.co.uk` | timeout · GB · job 16 | reachable | httpx 200 ok now |
| `fasttrackconsultancy.com` | timeout · GB · job 16 | reachable | httpx 200 ok now |
| `bcsolicitors.co.uk` | timeout · GB · job 16 | reachable | httpx 200 ok now |

## Connection failures (was 'network'; now tls / reset / refused)

W56 splits these. `tls` = certificate / handshake (the browser proceeds past cert errors), `reset` = the host drops non-browser clients, `refused` = nothing listening.

| domain | why | expect | result at generation |
|---|---|---|---|
| `browningsgarage.co.uk` | network · GB · job 7 | reachable | still 403 [cloudflare,cf_challenge] |
| `suttonorthodonticcentre.co.uk` | network · GB · job 14 | reachable | still network |
| `pittstcosmetic.com.au` | network · AU · job 15 | reachable | still timeout · browser network |
| `bellapellecc.com.au` | network · AU · job 15 | reachable | still timeout · browser network |
| `csdermatology.com.au` | network · AU · job 15 | reachable | still timeout · browser network |
| `sydneychirocare.com.au` | network · AU · job 15 | reachable | still timeout · browser timeout |
| `ovalphysio.com.au` | network · AU · job 15 | reachable | still timeout · browser network |
| `hplpsolicitors.co.uk` | network · GB · job 16 | reachable | httpx 200 ok now |
| `travelvisaagency.co.uk` | network · GB · job 16 | reachable | httpx 200 ok now |
| `ready-visa.com` | network · GB · job 16 | reachable | still network |

## Server errors (5xx)

Origin errors; Cloudflare 52x means the origin is down behind Cloudflare. Mostly transient — a later re-run passes on its own.

| domain | why | expect | result at generation |
|---|---|---|---|
| `cvhaccountants.co.uk` | http_500 · GB · job 16 | reachable | httpx 200 ok now |
| `taxserve.co.uk` | http_500 · GB · job 16 | reachable | still 500 [cloudflare] |
| `buttonart.in` | http_523 · IN · job 26 | reachable | still timeout · browser network |
| `esqube.in` | http_525 · IN · job 35 | reachable | still 520 [cloudflare] |
| `qvcuk.com` | http_504 · GB · job 36 | reachable | httpx 200 ok now |
| `high.inc` | http_523 · GB · job 44 | reachable | still 523 [cloudflare] |

## DNS failures (dead or misspelled hosts)

The biggest failure bucket. W56 tries the www. ⇄ bare variant once; what remains is genuinely dead and the list expects it to STAY dead — a pass here means the domain came back and the row should move.

| domain | why | expect | result at generation |
|---|---|---|---|
| `autofinancedirect.co.uk` | dns · GB · job 7 | dead | still dns |
| `akaestheticcentre.co.uk` | dns · GB · job 14 | dead | still dns |
| `allureaestheticclinic.com` | dns · GB · job 14 | dead | still dns |
| `srsequipments.com` | dns · IN · job 35 | dead | still dns |
| `aroraindustrialfastners.com` | dns · IN · job 35 | dead | still dns |
| `binayakfils.com` | dns · IN · job 35 | dead | still dns |
| `welgreen.in` | dns · IN · job 35 | dead | still dns |
| `rsindustry.in` | dns · IN · job 35 | dead | httpx 200 ok now |
| `karaliindustries.com` | dns · IN · job 35 | dead | still dns |
| `intekhoist.com` | dns · IN · job 35 | dead | still dns |

## Page gone (404 / 410)

Maps holds a deep link that no longer exists. Home page may still answer — the crawler follows the listed URL, so these stay 'dead' for regression purposes.

| domain | why | expect | result at generation |
|---|---|---|---|
| `luxurycarsltd.co.uk` | http_404 · GB · job 6 | dead | still 404 [wix,wix] |
| `theformulaclinic.com` | http_404 · GB · job 14 | dead | still 404 [cloudflare,wix,wix] |
| `karishmatiles.com` | http_404 · IN · job 35 | dead | still 404 [litespeed] |
| `tragoindia.com` | http_404 · IN · job 35 | dead | still 404 [cloudflare,wix,wix] |
| `musclefactorygym.wix.com` | http_404 · IN · job 35 | dead | still 404 [cloudflare,wix,wix] |

## Done but nothing extracted (JS shells, builders, contact data off the home page)

1,852 leads were marked done with no email / social / phone. Causes seen: JS-only app shells (need the browser), Wix/Squarespace with contact only in JSON-LD, sites whose only contact page is not linked as 'contact'. W56: JS shells go straight to the browser, JSON-LD is read, contact links are ranked.

| domain | why | expect | result at generation |
|---|---|---|---|
| `kensingtondental.co.uk` | done · GB · job 2 | any | httpx 200 ok now |
| `frankharris.co.uk` | done · GB · job 5 | any | httpx 200 ok now |
| `thundersub.com` | done · GB · job 7 | any | httpx 200 · contact page e0/p1 |
| `fuelhero.co.uk` | done · GB · job 7 | any | httpx 200 · JSON-LD e0/p1/s0 · contact page e0/p1 |
| `hypnotherapy2empower.com` | done · GB · job 11 | any | httpx 200 |
| `easternsuburbsderm.com.au` | done · AU · job 15 | any | httpx 200 · contact page e1/p2 |
| `pkbcs.co.uk` | done · GB · job 16 | any | httpx 200 · JS shell |
| `vc.tasheer.com` | done · GB · job 16 | any | httpx 403 |
| `sardartextiles.website2.me` | done · IN · job 26 | any | httpx 200 |
| `parasfashion.co.in` | done · IN · job 26 | any | httpx 200 · JSON-LD e0/p1/s0 · contact page e0/p1 |
| `askrealtors.in` | done · IN · job 30 | any | httpx 200 |
| `paradigmartteza.com` | done · IN · job 30 | any | httpx 200 |
| `arthrealty.com` | done · IN · job 30 | any | httpx 200 · JS shell |
| `juhujvpd.blogspot.com` | done · IN · job 30 | any | httpx 200 |
| `ibinfra.in` | done · IN · job 30 | any | httpx 503 |

## Thin pages

Home page under 2 KB with nothing on it — usually a redirect stub or a splash page. The ranked contact-link pass is the fix.

| domain | why | expect | result at generation |
|---|---|---|---|
| `growelimpex.com` | http_403 · IN · job 26 | any | httpx 200 ok now |
| `intercarsltd.co.uk` | thin · GB · job 6 | any | not re-probed |
| `hertz.co.uk` | thin · GB · job 7 | any | not re-probed |
| `asruk.co.uk` | thin · GB · job 7 | any | not re-probed |
| `bidspotter.co.uk` | thin · GB · job 7 | any | not re-probed |
| `trumpingtonstreetdentistry.co.uk` | thin · GB · job 11 | any | not re-probed |

## Clean sites that must always pass

Read by plain httpx with email + socials found. If any of these regress, the header set / extractor changed something it should not have.

| domain | why | expect | result at generation |
|---|---|---|---|
| `houseofaas.com` | httpx, email + socials · IN | email | httpx 200 ok now |
| `turnerswim.co.uk` | httpx, email + socials · GB | email | still 403 [cloudflare,cf_challenge] |
| `harleystreetdentalclinic.co.uk` | httpx, email + socials · GB | email | not re-probed |
| `londoncitysmiles.com` | httpx, email + socials · GB | email | not re-probed |
| `thekensingtondentist.com` | httpx, email + socials · GB | email | not re-probed |
| `24hour-emergencydentist.co.uk` | httpx, email + socials · GB | email | not re-probed |
| `toothlondon.co.uk` | httpx, email + socials · GB | email | not re-probed |
| `americansmile.co.uk` | httpx, email + socials · GB | email | not re-probed |
| `thedentalsurgery.co.uk` | httpx, email + socials · GB | email | not re-probed |
| `bonddental.co.uk` | httpx, email + socials · GB | email | not re-probed |
| `thelondondentalcentre.co.uk` | httpx, email + socials · GB | email | not re-probed |
| `londonsmiling.com` | httpx, email + socials · GB | email | not re-probed |
| `dentalbeautyislington.co.uk` | httpx, email + socials · GB | email | not re-probed |
| `camdenhighstreetpractice.co.uk` | httpx, email + socials · GB | email | not re-probed |
| `londoncosmeticdentistry.co.uk` | httpx, email + socials · GB | email | not re-probed |

## Social-link edge cases

Pure-extractor cases, covered by `tests/test_extractors.py` + `tests/test_w56_blocked_sites.py` rather than the network: share/intent widgets (`facebook.com/sharer`, `twitter.com/intent`), bare `instagram.com` footer links, `linkedin.com/company/…` vs `/in/…`, JSON-LD `sameAs` arrays, `profile.php?id=` pages.
