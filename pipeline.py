#!/usr/bin/env python3
"""insta-tapes — Instagram profile -> video transcripts (Groq whisper) + full comments.
Runs ONLY on GitHub Actions (clean Azure IP; our own egress is login-walled).
Strategies logged per step; every fallback is explicit in the log.
"""
import os, re, sys, json, time, html, subprocess, urllib.parse

PROFILE_URL = sys.argv[1] if len(sys.argv) > 1 else "https://www.instagram.com/androo.agi"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
IG_APP_ID = "996165915644281"
ASBD = "129477"
GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
os.makedirs(OUT, exist_ok=True)
LOGF = open(os.path.join(OUT, "run.log"), "a", buffering=1)

from curl_cffi import requests as cr
S = cr.Session(impersonate="chrome")

def log(kind, msg):
    line = "%s|%s" % (kind, msg)
    print(line, flush=True)
    LOGF.write(line + "\n")

# ---------------- json block extraction (probe v5 technique) ----------------
def match_block(text, start, open_c, close_c):
    depth, i, in_str, esc = 0, start, False, False
    while i < len(text):
        c = text[i]
        if in_str:
            if esc: esc = False
            elif c == "\\": esc = True
            elif c == '"': in_str = False
        else:
            if c == '"': in_str = True
            elif c == open_c: depth += 1
            elif c == close_c:
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        i += 1
    return None

def grab_json(page, anchor_re):
    m = re.search(anchor_re, page)
    if not m:
        return None
    i = page.find("{", m.end() - 1)
    if i < 0:
        i = page.find("[", m.end() - 1)
    if i < 0:
        return None
    o = "{" if page[i] == "{" else "["
    c = "}" if o == "{" else "]"
    blk = match_block(page, i, o, c)
    if not blk:
        return None
    try:
        return json.loads(blk)
    except Exception:
        return None

def http_get(url, tries=4, ua=True, **kw):
    kw.setdefault("timeout", 40)
    hdrs = kw.setdefault("headers", {})
    if ua:
        hdrs["User-Agent"] = UA
    for a in range(tries):
        try:
            r = S.get(url, **kw)
            if r.status_code == 200:
                return r
            last = "HTTP%d" % r.status_code
        except Exception as e:
            last = str(e)[:80]
        time.sleep(2 + a * 3)
    log("WARN", "get fail %s -> %s" % (url[:90], last))
    return None

# ---------------- PHASE 1: profile -> post list ----------------
def phase_profile():
    r = None
    strategies = []
    # S1: the URL as given (stkn share token may bypass the wall)
    r = http_get(PROFILE_URL)
    strategies.append(("share_url", r is not None))
    page = r.text if r else ""
    username = (re.search(r"instagram\.com/([A-Za-z0-9_.]+)/?", PROFILE_URL) or [None, ""])[1] \
        if re.search(r"instagram\.com/([A-Za-z0-9_.]+)/?", PROFILE_URL) else ""
    uid = None
    codes = []   # list of dicts: {code, type}

    def add(code, typ="p"):
        if code and not any(c["code"] == code for c in codes):
            codes.append({"code": code, "type": typ})

    # parse embedded structures from page (whichever exists)
    def parse_page(pg):
        found_uid = None
        # user id candidates
        m = re.search(r'"(?:user_id|id|pk)"\s*:\s*"(\d{5,15})"', pg)
        if m: found_uid = m.group(1)
        # web_profile_info style
        j = grab_json(pg, r'"xdt_api__v1__users__web_profile_info"')
        if isinstance(j, dict):
            user = (j.get("xdt_api__v1__users__web_profile_info") or {}).get("user") or {}
            if user.get("id"): found_uid = user.get("id")
            med = ((user.get("edge_owner_to_timeline_media") or {})
                   or (user.get("media") or {}))
            for n in (med.get("edges") or []):
                node = n.get("node") or n
                add(node.get("code"), "reel" if node.get("is_video") or node.get("__typename") == "GraphVideo" else "p")
            for n in (med.get("nodes") or []):
                add(n.get("code"), "reel" if n.get("is_video") else "p")
        # classic sharedData
        j2 = grab_json(pg, r'window\._sharedData\s*=')
        if isinstance(j2, dict):
            try:
                entry = j2["entry_data"]["ProfilePage"][0]["graphql"]["user"]
                found_uid = found_uid or entry.get("id")
                med = entry.get("edge_owner_to_timeline_media") or {}
                for e in med.get("edges", []):
                    node = e.get("node") or {}
                    add(node.get("shortcode"), "reel" if node.get("is_video") else "p")
            except Exception:
                pass
        # raw shortcode sweep (last resort, grid SSR)
        for m in re.finditer(r'"(?:shortcode|code)"\s*:\s*"([A-Za-z0-9_-]{5,30})"', pg):
            add(m.group(1), "p")
        return found_uid

    uid = parse_page(page)
    log("PHASE1", "share_url len=%d uid=%s codes=%d" % (len(page), uid, len(codes)))

    # S2: web_profile_info API (usually anon-OK with app-id header)
    if len(codes) < 4 and username:
        r2 = http_get("https://www.instagram.com/api/v1/users/web_profile_info/?username=" + username,
                      headers={"User-Agent": UA, "x-ig-app-id": IG_APP_ID, "Accept": "*/*"})
        if r2:
            try:
                user = (r2.json()["data"]["user"] or {})
                uid = uid or user.get("id")
                med = user.get("edge_owner_to_timeline_media") or user.get("media") or {}
                for e in (med.get("edges") or []):
                    node = e.get("node") or {}
                    add(node.get("shortcode") or node.get("code"),
                        "reel" if node.get("is_video") else "p")
                for n in (med.get("nodes") or []):
                    add(n.get("code") or n.get("shortcode"), "reel" if n.get("is_video") else "p")
                log("PHASE1", "web_profile_info codes=%d" % len(codes))
            except Exception as e:
                log("PHASE1", "web_profile_info parse fail: %s" % str(e)[:100])
        strategies.append(("web_profile_info", r2 is not None))

    # S3: ?__a=1&__d=dis
    if len(codes) < 4 and username:
        r3 = http_get("https://www.instagram.com/%s/?__a=1&__d=dis" % username,
                      headers={"User-Agent": UA, "Accept": "application/json"})
        if r3:
            try:
                j = r3.json()
                graphql = j.get("graphql") or (j.get("data") or {}).get("user") or {}
                med = graphql.get("edge_owner_to_timeline_media") or graphql.get("media") or {}
                for e in (med.get("edges") or []):
                    node = e.get("node") or {}
                    add(node.get("shortcode") or node.get("code"), "reel" if node.get("is_video") else "p")
                for n in (med.get("nodes") or []):
                    add(n.get("code") or n.get("shortcode"), "reel" if n.get("is_video") else "p")
                log("PHASE1", "a1d codes=%d" % len(codes))
            except Exception as e:
                log("PHASE1", "a1d parse fail: %s" % str(e)[:100])

    # S4: GraphQL pagination for the FULL grid
    if uid:
        codes_count_before = len(codes)
        claim = ""
        try:
            claim = S.response.headers.get("x-ig-set-www-claim", "") or ""
        except Exception:
            claim = ""
        # doc_id discovery from page source
        doc_ids = re.findall(r'"(?:doc_id|query_id)"\s*:\s*"(\d{5,20})"', page)
        doc_ids = list(dict.fromkeys(doc_ids))
        log("PHASE1", "doc_ids found=%d %s" % (len(doc_ids), doc_ids[:6]))
        cursor = None
        pages = 0
        while pages < 20:
            pages += 1
            # pick a doc_id: try in order (first = profile grid typically)
            got_page = False
            for did in (doc_ids or [""]):
                if not did:
                    break
                variables = {"id": str(uid), "first": 50}
                if cursor:
                    variables["after"] = cursor
                body = "variables=" + urllib.parse.quote(json.dumps(variables)) + "&doc_id=" + did
                try:
                    rr = S.post("https://www.instagram.com/api/graphql", data=body, timeout=40, headers={
                        "User-Agent": UA, "x-ig-app-id": IG_APP_ID, "x-asbd-id": ASBD,
                        "x-ig-www-claim": claim, "content-type": "application/x-www-form-urlencoded",
                        "Origin": "https://www.instagram.com", "Referer": PROFILE_URL,
                    })
                except Exception as e:
                    log("PHASE1", "gql err %s" % str(e)[:80])
                    continue
                if rr.status_code != 200:
                    log("PHASE1", "gql HTTP%d doc=%s" % (rr.status_code, did))
                    continue
                try:
                    j = rr.json()
                except Exception:
                    log("PHASE1", "gql non-json doc=%s" % did)
                    continue
                if "login" in json.dumps(j)[:400].lower() and not re.search(r'"code"', json.dumps(j)[:2000]):
                    log("PHASE1", "gql login-required doc=%s" % did)
                    continue
                data = j.get("data") or {}
                media = None
                for k, v in (data.items() if isinstance(data, dict) else []):
                    if isinstance(v, dict):
                        m2 = v.get("edge_owner_to_timeline_media") or v.get("media") or v.get("xdt_api__v1__media__shortcode__web_info")
                        if isinstance(m2, dict) and (m2.get("edges") or m2.get("nodes")):
                            media = m2
                            break
                if not media:
                    continue
                before = len(codes)
                for e in (media.get("edges") or []):
                    node = e.get("node") or {}
                    add(node.get("shortcode") or node.get("code"), "reel" if node.get("is_video") else "p")
                for n in (media.get("nodes") or []):
                    add(n.get("code") or n.get("shortcode"), "reel" if n.get("is_video") else "p")
                pg = media.get("page_info") or {}
                cursor = pg.get("end_cursor")
                got_page = True
                log("PHASE1", "gql page=%d doc=%s +%d total=%d next=%s" %
                    (pages, did, len(codes) - before, len(codes), bool(pg.get("has_next_page"))))
                if not pg.get("has_next_page") or not cursor:
                    cursor = None
                    break
                break
            if not got_page or not cursor:
                break
        strategies.append(("graphql_pagination", len(codes) > codes_count_before))

    log("PHASE1", "TOTAL codes=%d strategies=%s" % (len(codes), strategies))
    return codes

def phase_profile_v2():
    """Forensics-first: log what the page actually contains, then escalate
    share_url -> ?__a=1 -> web_profile_info -> headless Chrome render."""
    username = ""
    m_u = re.search(r"instagram\.com/([A-Za-z0-9_.]+)/?", PROFILE_URL)
    if m_u:
        username = m_u.group(1)
    codes = []

    def add(code, typ="p"):
        if not code or code in ("en_US", "us", "web", "login", "accounts"):
            return
        if not any(c["code"] == code for c in codes):
            codes.append({"code": code, "type": typ})

    def harvest_html(html, tag):
        n0 = len(codes)
        for m in re.finditer(r'href="[^"]*/(p|reel|tv)/([A-Za-z0-9_-]{5,40})/', html or ""):
            add(m.group(2), "reel" if m.group(1) in ("reel", "tv") else "p")
        for m in re.finditer(r'"shortcode"\s*:\s*"([A-Za-z0-9_-]{5,40})"', html or ""):
            add(m.group(1), "p")
        log("PHASE1", "%s +%d -> total=%d" % (tag, len(codes) - n0, len(codes)))

    def sweep_json(blob, tag):
        n0 = len(codes)
        for m in re.finditer(r'"shortcode"\s*:\s*"([A-Za-z0-9_-]{5,40})"', blob or ""):
            add(m.group(1), "p")
        if len(codes) != n0:
            log("PHASE1", "%s json +%d -> total=%d" % (tag, len(codes) - n0, len(codes)))

    # ---- S1: share URL + forensics dump
    r = http_get(PROFILE_URL)
    page = r.text if r else ""
    log("PHASE1", "share_url len=%d shortcode_keys=%d p_href=%d login_wall=%s" % (
        len(page), len(re.findall(r'"shortcode"', page)),
        len(re.findall(r'href="[^"]*/(?:p|reel|tv)/', page)),
        "/accounts/login" in page[:3000]))
    if page:
        harvest_html(page, "share_html")
        blobs = re.findall(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', page, re.S)
        log("PHASE1", "json_blobs=%d" % len(blobs))
        for i, bl in enumerate(blobs[:12]):
            try:
                j = json.loads(bl)
                keys = list(j.keys())[:8] if isinstance(j, dict) else ["<list:%d>" % len(j)]
                log("PHASE1", "blob%d keys=%s len=%d" % (i, keys, len(bl)))
            except Exception:
                pass
    # S1b: retry share URL with ONE coherent fingerprint (no custom UA header)
    if len(codes) < 4:
        try:
            r0b = http_get(PROFILE_URL, ua=False, tries=2)
            page_b = r0b.text if r0b else ""
            wall_b = "/accounts/login" in page_b[:3000]
            n_sc = len(re.findall(r'"shortcode"', page_b))
            log("PHASE1", "share_noua len=%d wall=%s shortcodes=%d" % (len(page_b), wall_b, n_sc))
            if page_b and not wall_b:
                page = page_b
                harvest_html(page_b, "share_noua")
        except Exception as e:
            log("PHASE1", "share_noua err %s" % str(e)[:100])
    claim = ""
    try:
        claim = S.response.headers.get("x-ig-set-www-claim", "") or ""
    except Exception:
        pass

    # ---- S2: ?__a=1&__d=dis — accept ANY 2xx, log the body head
    if len(codes) < 4 and username:
        try:
            r2 = S.get("https://www.instagram.com/%s/?__a=1&__d=dis" % username,
                       timeout=30, headers={"User-Agent": UA, "Accept": "application/json"})
            log("PHASE1", "a1 status=%d head=%r" % (r2.status_code, r2.text[:220]))
            if r2.status_code < 300:
                sweep_json(r2.text, "a1")
        except Exception as e:
            log("PHASE1", "a1 err %s" % str(e)[:100])

    # ---- S3: web_profile_info with claim + browser headers
    if len(codes) < 4 and username:
        try:
            r3 = S.get("https://www.instagram.com/api/v1/users/web_profile_info/?username=" + username,
                       timeout=30, headers={"User-Agent": UA, "x-ig-app-id": IG_APP_ID,
                                            "x-asbd-id": ASBD, "x-ig-www-claim": claim,
                                            "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9",
                                            "Referer": PROFILE_URL})
            log("PHASE1", "wpi status=%d head=%r" % (r3.status_code, r3.text[:220]))
            if r3.status_code == 200:
                sweep_json(r3.text, "wpi")
        except Exception as e:
            log("PHASE1", "wpi err %s" % str(e)[:100])

    # ---- S3b: web_profile_info without custom UA + GraphQL grid pagination
    if len(codes) < 6 and username:
        wpi_text = ""
        try:
            r4 = S.get("https://www.instagram.com/api/v1/users/web_profile_info/?username=" + username,
                       timeout=30, headers={"x-ig-app-id": IG_APP_ID, "x-asbd-id": ASBD,
                                            "Accept": "*/*", "Referer": PROFILE_URL})
            log("PHASE1", "wpi2 status=%d head=%r" % (r4.status_code, r4.text[:200]))
            if r4.status_code == 200:
                wpi_text = r4.text
                sweep_json(wpi_text, "wpi2")
        except Exception as e:
            log("PHASE1", "wpi2 err %s" % str(e)[:100])
        m_uid = re.search(r'"(?:user_id|id|pk)"\s*:\s*"(\d{5,15})"', wpi_text or "")
        uid = m_uid.group(1) if m_uid else None
        log("PHASE1", "uid=%s codes=%d" % (uid, len(codes)))
        if uid and len(codes) < 6:
            doc_ids = list(dict.fromkeys(re.findall(r'"(?:doc_id|query_id)"\s*:\s*"(\d{5,20})"', page or "")))
            if not doc_ids:
                bundles = list(dict.fromkeys(re.findall(r'src="(https://[^"]+\.js)"', page or "")))[:10]
                log("PHASE1", "bundles=%d" % len(bundles))
                for bu in bundles:
                    try:
                        rb = S.get(bu, timeout=30)
                        if rb.status_code == 200:
                            doc_ids.extend(re.findall(r'doc_id["\x27]?\s*[:=]\s*["\x27](\d{5,20})["\x27]', rb.text))
                    except Exception:
                        pass
                    if len(doc_ids) > 30:
                        break
                doc_ids = list(dict.fromkeys(doc_ids))
            log("PHASE1", "doc_ids=%d %s" % (len(doc_ids), doc_ids[:8]))
            cursor = None
            ok_doc = None
            for rnd in range(24):
                hit = False
                for did in (doc_ids if ok_doc is None else [ok_doc]):
                    if not did:
                        break
                    variables = {"id": str(uid), "first": 50}
                    if cursor:
                        variables["after"] = cursor
                    body_ = "variables=" + urllib.parse.quote(json.dumps(variables)) + "&doc_id=" + did
                    try:
                        rq_ = S.post("https://www.instagram.com/api/graphql", data=body_, timeout=40,
                                     headers={"x-ig-app-id": IG_APP_ID, "x-asbd-id": ASBD,
                                              "x-ig-www-claim": claim,
                                              "content-type": "application/x-www-form-urlencoded",
                                              "Origin": "https://www.instagram.com", "Referer": PROFILE_URL})
                    except Exception as e:
                        log("PHASE1", "gql err %s" % str(e)[:80])
                        continue
                    if rq_.status_code != 200:
                        log("PHASE1", "gql HTTP%d doc=%s" % (rq_.status_code, did))
                        continue
                    txt = rq_.text
                    if '"login"' in txt[:800]:
                        log("PHASE1", "gql login-wall doc=%s head=%r" % (did, txt[:150]))
                        continue
                    n0 = len(codes)
                    sweep_json(txt, "gql")
                    if len(codes) == n0:
                        continue
                    ok_doc = did
                    cm = re.search(r'"end_cursor"\s*:\s*"([^"]+)"', txt)
                    hm = re.search(r'"has_next_page"\s*:\s*(true|false)', txt)
                    cursor = cm.group(1) if (cm and hm and hm.group(1) == "true") else None
                    log("PHASE1", "gql r%d doc=%s +%d total=%d next=%s" %
                        (rnd, did, len(codes) - n0, len(codes), bool(cursor)))
                    hit = True
                    break
                if not hit or not cursor:
                    break
                time.sleep(0.5)
        log("PHASE1", "after graphql codes=%d" % len(codes))

    # ---- S4: headless Chrome render (runner has system Chrome) + scroll pagination
    if len(codes) < 4:
        try:
            import subprocess as sp
            sp.run([sys.executable, "-m", "pip", "install", "-q", "playwright"],
                   check=True, timeout=240, capture_output=True)
            from playwright.sync_api import sync_playwright
            log("PHASE1", "playwright up — launching system chrome")
            stkn = ""
            pq = urllib.parse.parse_qs(urllib.parse.urlparse(PROFILE_URL).query)
            if pq.get("stkn"):
                stkn = "?stkn=" + pq["stkn"][0]
            base = PROFILE_URL.split("?")[0].rstrip("/")
            with sync_playwright() as pw:
                br = pw.chromium.launch(channel="chrome", headless=True,
                                        args=["--disable-blink-features=AutomationControlled",
                                              "--no-sandbox"])
                ctx = br.new_context(user_agent=UA, viewport={"width": 1366, "height": 900},
                                     locale="en-US")
                pg = ctx.new_page()

                def render_walk(url, rounds, tag):
                    pg.goto(url, wait_until="domcontentloaded", timeout=60000)
                    pg.wait_for_timeout(3500)
                    try:
                        log("PHASE1", "%s landed url=%s title=%r" % (tag, (pg.url or "")[:110], (pg.title() or "")[:60]))
                    except Exception:
                        pass
                    for sel in ['button:has-text("Not now")', 'button:has-text("Cancel")',
                                'div[role="dialog"] button[aria-label="Close"]']:
                        try:
                            loc = pg.locator(sel).first
                            if loc.is_visible(timeout=600):
                                loc.click(timeout=1500)
                                log("PHASE1", "%s dismissed: %s" % (tag, sel))
                                break
                        except Exception:
                            pass
                    streak, last = 0, -1
                    for i in range(rounds):
                        harvest_html(pg.content(), "%s_r%d" % (tag, i))
                        if len(codes) == last:
                            streak += 1
                        else:
                            streak, last = 0, len(codes)
                        if streak >= 4 and i >= 3:
                            break
                        pg.mouse.wheel(0, 5000)
                        pg.wait_for_timeout(1100)

                render_walk(PROFILE_URL, 40, "chrome")
                if len(codes) < 5:
                    render_walk(base + "/reels/" + stkn, 20, "reels_tab")
                br.close()
            log("PHASE1", "chrome done codes=%d" % len(codes))
        except Exception as e:
            log("PHASE1", "chrome FAIL: %s" % str(e)[:300])

    log("PHASE1", "TOTAL codes=%d" % len(codes))
    return codes

GRID_INFO = {}

def phase_profile_v3():
    """Grid-first (Scrapfly 2026 recipe): GET graphql/query with the account
    doc_id + relay variables. Independent of the (often walled) profile HTML.
    Falls back to v2 strategies when the grid endpoint refuses."""
    global GRID_INFO
    username = ""
    m_u = re.search(r"instagram\.com/([A-Za-z0-9_.]+)/?", PROFILE_URL)
    if m_u:
        username = m_u.group(1)
    if not username:
        log("PHASE1", "no username parsed from URL"); return []

    codes = []
    def add(code, typ="p"):
        if code and code not in ("en_US", "us", "web", "login") and \
           not any(c["code"] == code for c in codes):
            codes.append({"code": code, "type": typ})

    # best-effort profile page: loose doc_ids + is_private (page may be walled)
    page = ""
    r0 = http_get(PROFILE_URL)
    if r0: page = r0.text or ""
    wall = ("/accounts/login" in (getattr(r0, "url", "") or "")) if r0 else True
    log("PHASE1", "profile page len=%d wall=%s xig=%s" % (
        len(page), wall, "xig_user_by_username" in page))
    if "xig_user_by_username" in page:
        m_priv = re.search(r'"is_private"\s*:\s*(true|false)', page)
        log("PHASE1", "is_private=%s" % (m_priv.group(1) if m_priv else "?"))

    docs = ["9310670392322965"]  # Scrapfly constant: account grid doc_id
    extra = re.findall(r'doc_id[\s:="]*(\d{12,20})', page)
    for bu in re.findall(r'src="(https://[^"]+\.js)"', page)[:8]:
        try:
            rb = S.get(bu, timeout=30)
            if rb.status_code == 200:
                extra += re.findall(r'doc_id[\s:="]*(\d{12,20})', rb.text)
        except Exception:
            pass
    for d in dict.fromkeys(extra):
        if d not in docs: docs.append(d)
    log("PHASE1", "grid docs to try: %s" % docs[:6])

    def grid_params(count):
        return {"after": None, "before": None,
                "data": {"count": count, "include_reel_media_seen_timestamp": True,
                         "include_relationship_info": True,
                         "latest_besties_reel_media": True, "latest_reel_media": True},
                "first": count, "last": None, "username": username,
                "__relay_internal_pv__PolarisIsLoggedInrelayprovider": True,
                "__relay_internal_pv__PolarisShareSheetV3relayprovider": True}

    counts = [50, 12]
    picked = None
    prev_cursor = None
    for ci, cnt in enumerate(counts):
        variables = grid_params(cnt)
        for rnd in range(60):
            hit = False
            for did in ([picked] if picked else docs):
                params = {"doc_id": did,
                          "variables": json.dumps(variables, separators=(",", ":"))}
                url = "https://www.instagram.com/graphql/query/?" + urllib.parse.urlencode(params)
                r = http_get(url, ua=False, tries=2, headers={
                    "content-type": "application/x-www-form-urlencoded",
                    "Accept-Language": "en-US,en;q=0.9",
                    "x-ig-app-id": IG_APP_ID,
                    "Referer": PROFILE_URL})
                if not r or r.status_code != 200:
                    log("PHASE1", "grid HTTP %s doc=%s cnt=%d" % (
                        (r.status_code if r else "?"), did, cnt)); continue
                txt = r.text
                if '"require_login"' in txt[:400] or '"login"' in txt[:400]:
                    log("PHASE1", "grid gated doc=%s head=%r" % (did, txt[:150])); continue
                try:
                    j = json.loads(txt)
                except Exception:
                    log("PHASE1", "grid non-json doc=%s %r" % (did, txt[:120])); continue
                def find_conn(o):
                    if isinstance(o, dict):
                        if "edges" in o and "page_info" in o: return o
                        for v in o.values():
                            got = find_conn(v)
                            if got: return got
                    elif isinstance(o, list):
                        for v in o:
                            got = find_conn(v)
                            if got: return got
                    return None
                conn = find_conn(j)
                if not conn or not conn.get("edges"):
                    log("PHASE1", "grid empty doc=%s keys=%s" % (
                        did, list(j.get("data", {}).keys())[:4] if isinstance(j, dict) else "?"))
                    continue
                n0 = len(codes)
                for e in conn["edges"]:
                    nd = e.get("node") or {}
                    c = nd.get("code") or nd.get("shortcode") or ""
                    if not c: continue
                    vurls = []
                    vv = nd.get("video_versions")
                    if isinstance(vv, dict):
                        vurls = [x.get("url") for x in (vv.get("candidates") or []) if x.get("url")]
                    elif isinstance(vv, list):
                        vurls = [x.get("url") for x in vv if isinstance(x, dict) and x.get("url")]
                    cap = nd.get("caption")
                    if isinstance(cap, dict): cap = cap.get("text") or ""
                    im = nd.get("image_versions2") or {}
                    iurl = ""
                    if isinstance(im, dict):
                        cands = im.get("candidates") or []
                        if cands: iurl = cands[0].get("url") or ""
                    typename = str(nd.get("__typename") or nd.get("media_type") or "")
                    typ = "reel" if ("Reel" in typename or typename == "2") else "p"
                    GRID_INFO[c] = {"video_urls": [u for u in vurls if u],
                                    "image_url": iurl or nd.get("display_uri") or "",
                                    "captions": [cap] if cap else [],
                                    "likes": nd.get("like_count"),
                                    "taken_at": nd.get("taken_at"),
                                    "comments_count": nd.get("comment_count")}
                    add(c, typ)
                pi = conn.get("page_info") or {}
                log("PHASE1", "grid r%d doc=%s cnt=%d +%d total=%d next=%s" % (
                    rnd, did, cnt, len(codes) - n0, len(codes),
                    bool(pi.get("has_next_page"))))
                if len(codes) > n0: picked = did
                hit = True
                if not pi.get("has_next_page") or not pi.get("end_cursor"):
                    prev_cursor = "__end__"; break
                if pi.get("end_cursor") == prev_cursor:
                    log("PHASE1", "cursor stall, stopping"); prev_cursor = "__end__"; break
                prev_cursor = pi.get("end_cursor")
                variables["after"] = pi.get("end_cursor")
                break
            if (not hit) or prev_cursor == "__end__":
                break
            time.sleep(0.4)
        if prev_cursor == "__end__":
            break

    # Google-index seeds as safety net (public profile shortcodes)
    for sc in ["Dd7XBExRZQy", "DdvDpgnTEt3", "Ddt9JrXCLVq", "DWCCNE-jo12",
               "DdSG5a2oTs4", "DYOBxV2xGgJ", "DYpuXTckf0F"]:
        add(sc, "p")
    log("PHASE1", "TOTAL codes=%d (grid+seeds)" % len(codes))
    if len(codes) <= 7:
        log("PHASE1", "grid failed/empty — escalating to v2 strategies")
        for c2 in phase_profile_v2():
            add(c2.get("code"), c2.get("type") or "p")
    return codes

# ---------------- PHASE 2: per-post page ----------------
def phase_posts(codes):
    posts = []
    n = len(codes)
    for i, c in enumerate(codes, 1):
        code, typ = c["code"], c.get("type") or "p"
        url = "https://www.instagram.com/reel/%s/" % code if typ == "reel" else "https://www.instagram.com/p/%s/" % code
        r = http_get(url, tries=3)
        if not r:
            # try the other path once
            alt = "https://www.instagram.com/p/%s/" % code if typ == "reel" else "https://www.instagram.com/reel/%s/" % code
            r = http_get(alt, tries=2)
            if r:
                url = alt
        if not r:
            gi = GRID_INFO.get(code)
            if gi and (gi.get("video_urls") or gi.get("image_url")):
                caps = gi.get("captions") or [""]
                rec = {"code": code, "url": url,
                       "videos": list(gi.get("video_urls") or []),
                       "images": 1 if gi.get("image_url") else 0,
                       "caption": (caps[0] or "")[:4000],
                       "taken_at": gi.get("taken_at"), "like_count": gi.get("likes"),
                       "comment_count": gi.get("comments_count"),
                       "comments": [], "comment_count_found": 0,
                       "source": "grid_node"}
                log("POST", "%d/%d %s PAGE_FAIL->grid_node videos=%d" %
                    (i, n, code, len(rec["videos"])))
                posts.append(rec)
                continue
            log("POST", "%d/%d %s PAGE_FAIL" % (i, n, code))
            posts.append({"code": code, "url": url, "error": "page_fail"})
            continue
        page = r.text
        rec = {"code": code, "url": url}
        # media: carousel_media literal array first (Law 25: authoritative array)
        seq = []
        m = re.search(r'"carousel_media"\s*:\s*\[', page)
        if m:
            blk = match_block(page, page.index("[", m.start()), "[", "]")
            if blk:
                try:
                    seq = json.loads(blk)
                except Exception:
                    seq = []
        if not seq:
            # single media: video_versions / image_versions2 directly
            single = grab_json(page, r'"video_versions"')
            if isinstance(single, dict):
                seq = [single.get("video_versions") and {"video_versions": single} or single]
            if not seq:
                info = grab_json(page, r'"xdt_api__v1__media__shortcode__web_info"')
                if isinstance(info, dict):
                    med = (info.get("xdt_api__v1__media__shortcode__web_info") or {}).get("media") or {}
                    if med.get("carousel_media"):
                        seq = med["carousel_media"]
                    elif med:
                        seq = [med]
        videos = []
        images = 0
        for it in seq:
            if not isinstance(it, dict):
                continue
            vv = it.get("video_versions") or (it.get("video_url") and {"candidates": [{"url": it.get("video_url")}]} or None)
            if vv and (vv.get("candidates") or vv.get("url")):
                cands = vv.get("candidates") or [vv]
                u = cands[0].get("url")
                if u:
                    videos.append(u)
            elif it.get("image_versions2"):
                images += 1
        if not videos and GRID_INFO.get(code, {}).get("video_urls"):
            videos = list(GRID_INFO[code]["video_urls"])
            log("POST", "%s video url from grid node" % code)
        rec["videos"] = videos
        rec["images"] = images
        # caption
        cap = grab_json(page, r'"caption"\s*:\s*\{')
        if isinstance(cap, dict):
            rec["caption"] = (cap.get("text") or "")[:4000]
        elif GRID_INFO.get(code, {}).get("captions"):
            rec["caption"] = (GRID_INFO[code]["captions"][0] or "")[:4000]
        # meta timestamp/likes/comments
        for key, anchor in (("taken_at", r'"taken_at"\s*:\s*'), ("like_count", r'"like_count"\s*:\s*'),
                            ("comment_count", r'"comment_count"\s*:\s*')):
            m2 = re.search(anchor + r'(\d+)', page)
            if m2:
                rec[key] = int(m2.group(1))
        # comments: parent + threaded replies (embedded)
        coms, total_got = parse_comments(page)
        rec["comments"] = coms
        rec["comment_count_found"] = total_got
        log("POST", "%d/%d %s videos=%d comments=%d/%s" %
            (i, n, code, len(videos), total_got, rec.get("comment_count", "?")))
        posts.append(rec)
        time.sleep(0.8)
    return posts

def parse_comments(page):
    out = []
    j = grab_json(page, r'"edge_media_to_parent_comment"\s*:\s*\{')
    if isinstance(j, dict):
        edges = (j.get("edges") or [])
        for e in edges:
            node = e.get("node") or {}
            c = {
                "user": (node.get("owner") or {}).get("username") or "?",
                "text": (node.get("text") or ""),
                "likes": node.get("like_count") or 0,
                "id": node.get("id") or "",
                "replies": [],
            }
            th = node.get("edge_threaded_comments") or {}
            for re_ in (th.get("edges") or []):
                rn = re_.get("node") or {}
                c["replies"].append({
                    "user": (rn.get("owner") or {}).get("username") or "?",
                    "text": rn.get("text") or "",
                    "likes": rn.get("like_count") or 0,
                })
            out.append(c)
    return out, len(out)

# ---------------- PHASE 3: full comments via GraphQL (best-effort) ----------
def phase_comments_deep(posts, page_src):
    doc_ids = list(dict.fromkeys(re.findall(r'"(?:doc_id|query_id)"\s*:\s*"(\d{5,20})"', page_src or "")))
    if not doc_ids:
        log("COMMENTS", "no doc_ids — embedded only")
        return
    claim = ""
    try:
        claim = S.response.headers.get("x-ig-set-www-claim", "") or ""
    except Exception:
        pass
    deep_ok = 0
    for rec in posts:
        want = rec.get("comment_count") or 0
        have = rec.get("comment_count_found") or 0
        if not want or have >= want:
            continue
        media_id = None
        # media pk: from embedded shortcode page — we stored only comments; refetch cheap? skip if absent
        # GraphQL comments pagination needs media id + cursor; derive from stored comment ids (id = mediaid_commentid)
        if rec.get("comments") and rec["comments"][0].get("id"):
            media_id = str(rec["comments"][0]["id"]).split("_")[0]
        if not media_id:
            continue
        cursor = None
        for did in doc_ids:
            fetched_extra = []
            for _ in range(6):
                variables = {"shortcode": rec["code"], "first": 50}
                if cursor:
                    variables["after"] = cursor
                body = "variables=" + urllib.parse.quote(json.dumps(variables)) + "&doc_id=" + did
                try:
                    rr = S.post("https://www.instagram.com/api/graphql", data=body, timeout=40, headers={
                        "User-Agent": UA, "x-ig-app-id": IG_APP_ID, "x-asbd-id": ASBD,
                        "x-ig-www-claim": claim, "content-type": "application/x-www-form-urlencoded",
                        "Origin": "https://www.instagram.com",
                    })
                    j = rr.json()
                except Exception:
                    break
                edges = []
                data = j.get("data") or {}
                stack = [data]
                while stack and not edges:
                    cur = stack.pop()
                    if isinstance(cur, dict):
                        if "edges" in cur and isinstance(cur.get("edges"), list) and cur["edges"] and isinstance(cur["edges"][0], dict) and "node" in cur["edges"][0]:
                            edges = cur["edges"]
                        else:
                            stack.extend(cur.values())
                    elif isinstance(cur, list):
                        stack.extend(cur)
                if not edges:
                    break
                for e in edges:
                    node = e.get("node") or {}
                    fetched_extra.append({
                        "user": (node.get("owner") or {}).get("username") or "?",
                        "text": node.get("text") or "",
                        "likes": node.get("like_count") or 0,
                        "id": node.get("id") or "",
                        "replies": [],
                    })
                # cursor
                cursor = None
                stack = [j.get("data") or {}]
                while stack and not cursor:
                    cur = stack.pop()
                    if isinstance(cur, dict):
                        pi = cur.get("page_info") or cur.get("paging_info")
                        if isinstance(pi, dict) and pi.get("has_next_page") and pi.get("end_cursor"):
                            cursor = pi["end_cursor"]
                        else:
                            stack.extend(cur.values())
                    elif isinstance(cur, list):
                        stack.extend(cur)
                if not cursor:
                    break
                time.sleep(0.6)
            if fetched_extra:
                have_ids = {c.get("id") for c in rec["comments"]}
                add_n = 0
                for c in fetched_extra:
                    if c["id"] not in have_ids:
                        rec["comments"].append(c)
                        have_ids.add(c["id"])
                        add_n += 1
                if add_n:
                    rec["comment_count_found"] = len(rec["comments"])
                    deep_ok += 1
                    log("COMMENTS", "%s +%d -> %d/%s" % (rec["code"], add_n, len(rec["comments"]), want))
                break
        time.sleep(0.5)
    log("COMMENTS", "deep-paginated posts=%d" % deep_ok)

# ---------------- PHASE 4: video -> whisper ----------------
def transcribe(videos, tag):
    texts = []
    for vi, vurl in enumerate(videos, 1):
        ok = False
        for attempt in range(3):
            try:
                r = S.get(vurl, timeout=120, headers={"User-Agent": UA, "Referer": "https://www.instagram.com/"})
                if r.status_code != 200 or len(r.content) < 1024:
                    raise RuntimeError("video HTTP%d len=%d" % (r.status_code, len(r.content)))
                mp4 = "/tmp/v_%s_%d.mp4" % (tag, vi)
                wav = "/tmp/a_%s_%d.wav" % (tag, vi)
                open(mp4, "wb").write(r.content)
                # 16k mono wav; if huge, cap length is fine — groq caps 25MB (16k wav ~ 47h/25MB, no issue)
                subprocess.run(["ffmpeg", "-y", "-i", mp4, "-vn", "-ac", "1", "-ar", "16000",
                                "-f", "wav", wav], check=True, capture_output=True, timeout=300)
                if os.path.getsize(wav) > 24 * 1024 * 1024:
                    subprocess.run(["ffmpeg", "-y", "-i", wav, "-b:a", "48k",
                                    "/tmp/a_small.wav"], check=True, capture_output=True)
                    os.replace("/tmp/a_small.wav", wav)
                # whisper-large-v3 FIRST (Mo rule), turbo fallback
                text = groq_whisper(wav)
                texts.append(text)
                os.remove(mp4); os.remove(wav)
                ok = True
                log("WHISPER", "%s v%d chars=%d" % (tag, vi, len(text)))
                break
            except Exception as e:
                log("WARN", "%s v%d attempt%d: %s" % (tag, vi, attempt + 1, str(e)[:120]))
                time.sleep(4 + attempt * 4)
        if not ok:
            texts.append("")
            log("WHISPER", "%s v%d FAILED" % (tag, vi))
    return "\n\n".join(t for t in texts if t).strip()

_groq_model_tried = []
def groq_whisper(wav):
    import requests as rq
    order = ["whisper-large-v3", "whisper-large-v3-turbo"]
    last = ""
    for model in order:
        if model in _groq_model_tried and model != order[0]:
            pass
        for attempt in range(3):
            try:
                with open(wav, "rb") as fh:
                    resp = rq.post(
                        "https://api.groq.com/openai/v1/audio/transcriptions",
                        headers={"Authorization": "Bearer " + GROQ_KEY, "User-Agent": UA},
                        files={"file": (os.path.basename(wav), fh, "audio/wav")},
                        data={"model": model, "response_format": "json", "language": ""},
                        timeout=300,
                    )
                if resp.status_code == 200:
                    if model != order[0]:
                        log("WHISPER", "fallback model in use: %s" % model)
                    return resp.json().get("text", "")
                last = "HTTP%d %s" % (resp.status_code, resp.text[:150])
                if resp.status_code in (400, 404) and "model" in resp.text.lower():
                    break  # try next model
                if resp.status_code == 429:
                    time.sleep(8 + attempt * 8)
                    continue
                break
            except Exception as e:
                last = str(e)[:150]
                time.sleep(5)
        _groq_model_tried.append(model)
    log("WARN", "groq fail: %s" % last)
    raise RuntimeError("groq: " + last)

# ---------------- PHASE 5: outputs ----------------
def write_outputs(posts):
    tapes = open(os.path.join(OUT, "ANDROO_TAPES.txt"), "w", encoding="utf-8")
    chatter = open(os.path.join(OUT, "ANDROO_CHATTER.txt"), "w", encoding="utf-8")
    n_vid = n_txt = n_fail = n_posts = 0
    for i, rec in enumerate(posts, 1):
        if rec.get("error"):
            continue
        n_posts += 1
        head = "#%d %s" % (i, rec["url"])
        tapes.write("=" * 70 + "\n" + head + "\n")
        if rec.get("taken_at"):
            try:
                tapes.write("date: " + time.strftime("%Y-%m-%d %H:%M", time.gmtime(rec["taken_at"])) + " UTC\n")
            except Exception:
                pass
        if rec.get("caption"):
            tapes.write("caption: " + rec["caption"].replace("\n", " ")[:600] + "\n")
        if rec.get("videos"):
            tr = rec.get("transcript") or ""
            if tr:
                tapes.write("TRANSCRIPT:\n" + tr + "\n")
                n_txt += 1
            else:
                tapes.write("TRANSCRIPT: [failed]\n")
                n_fail += 1
        else:
            tapes.write("TRANSCRIPT: [no video — image/text post]\n")
        tapes.write("\n")
        chatter.write("=" * 70 + "\n" + head + "\n")
        chatter.write("comments: %d shown / %s total\n" % (rec.get("comment_count_found") or 0, rec.get("comment_count", "?")))
        for c in rec.get("comments") or []:
            chatter.write("- %s (%d likes): %s\n" % (c["user"], c["likes"], c["text"].replace("\n", " ")))
            for rp in c.get("replies") or []:
                chatter.write("    -> %s: %s\n" % (rp["user"], (rp["text"] or "").replace("\n", " ")))
        chatter.write("\n")
        if rec.get("videos"):
            n_vid += 1
    tapes.close(); chatter.close()
    vids = sum(len(r.get("videos") or []) for r in posts if not r.get("error"))
    comments = sum(r.get("comment_count_found") or 0 for r in posts if not r.get("error"))
    total_declared = sum(r.get("comment_count") or 0 for r in posts if not r.get("error"))
    manifest = {
        "profile": PROFILE_URL,
        "posts_discovered": len(posts),
        "posts_processed": n_posts,
        "posts_with_video": n_vid,
        "videos_transcribed_ok": n_txt,
        "transcripts_failed": n_fail,
        "comments_collected": comments,
        "comments_declared_total": total_declared,
    }
    json.dump(manifest, open(os.path.join(OUT, "manifest.json"), "w"), indent=2)
    log("DONE", json.dumps(manifest))
    return manifest

# ---------------- main ----------------
def main():
    log("RUN", "profile=%s" % PROFILE_URL)
    codes = phase_profile_v3()
    if not codes:
        log("FATAL", "no posts discovered — see strategies in log")
        sys.exit(2)
    # save grid early (resilience)
    json.dump(codes, open(os.path.join(OUT, "grid.json"), "w"), indent=1)
    posts = phase_posts(codes)
    json.dump(posts, open(os.path.join(OUT, "posts_raw.json"), "w"), ensure_ascii=False)
    # deep comments best-effort (uses last page html — refetch profile for doc_ids)
    try:
        r = http_get(PROFILE_URL, tries=2)
        phase_comments_deep(posts, r.text if r else "")
    except Exception as e:
        log("WARN", "deep comments skipped: %s" % str(e)[:120])
    # transcripts
    for i, rec in enumerate(posts, 1):
        if rec.get("videos") and not rec.get("error"):
            rec["transcript"] = transcribe(rec["videos"], rec["code"])
            json.dump(posts, open(os.path.join(OUT, "posts_raw.json"), "w"), ensure_ascii=False)
    write_outputs(posts)
    log("RUN", "ALL DONE")

if __name__ == "__main__":
    main()
