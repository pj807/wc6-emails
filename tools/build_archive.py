#!/usr/bin/env python3
"""WC6 "Emails from Garrett" archive builder.

Reads ticket-holder emails that GoHighLevel has ACTUALLY SENT (workflow 1.03, subjects starting "(WC6) - "),
cleans them for a public page and adds any new ones to emails.json. Read-only against GHL
(GET message export + one POST /contacts/search, which only reads).

How "sent" is decided:
  1. Find a few real ticket holders (tag event_warrior_con_6) and list the "(WC6) - " emails they received.
  2. For each subject not yet in the archive, read every email GHL sent in a window around that send time.
  3. It counts as sent only if MIN_RECIPIENTS or more different contacts got it (test sends never qualify).

Cleaning (public page): greeting line -> "Brother,"; links/buttons/URLs removed; unsubscribe footer and
everything after it removed; HTML -> plain text paragraphs. Two copies sent to different contacts must
match after cleaning (proves nothing personal is left in the body).

Safety: any "{{", "}}", "http", "www", ".com" or "@" left in the subject or body -> that email is SKIPPED,
nothing of it is published, and a note is written. A partly cleaned email is never published.

Usage:
  python3 build_archive.py --out ../wc6-emails/emails.json [--raw-dir DIR ...] [--dry-run]
  Token: env GHL_TOKEN, else macOS Keychain service "ghl-wuw-kpi" (read-only key).
Exit code 0 = fine (new or nothing new), 2 = at least one email skipped (see notes), 1 = GHL error.
"""
import argparse, datetime as dt, glob, html, json, os, re, statistics, subprocess, sys, time
import urllib.error, urllib.parse, urllib.request
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

LOC = os.environ.get("GHL_LOCATION", "BDeGaczWUBVqlvyWoI7l")
BASE = "https://services.leadconnectorhq.com"
TAG = "event_warrior_con_6"
PREFIX = "(WC6) - "
SINCE = "2026-11-06T00:00:00.000Z"
MIN_RECIPIENTS = 20
MAX_PAGES = 5   # up to 500 ticket holders
ET = ZoneInfo("America/New_York")
UNSAFE = re.compile(r"\{\{|\}\}|https?|www|\.com|@", re.I)
NOTES = []


def note(msg):
    NOTES.append(msg)
    print("NOTE:", msg, file=sys.stderr)


# ------------------------------------------------------------------ GHL (read-only)
def token():
    if os.environ.get("GHL_TOKEN"):
        return os.environ["GHL_TOKEN"].strip()
    r = subprocess.run(["security", "find-generic-password", "-s", "ghl-wuw-kpi", "-w"], capture_output=True, text=True)
    return r.stdout.strip()


TOK = None


def req(method, path, params=None, body=None, version="2021-07-28"):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    data = json.dumps(body).encode() if body is not None else None
    for i in range(6):
        r = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": "Bearer " + TOK, "Version": version, "Accept": "application/json",
            "Content-Type": "application/json", "User-Agent": "wc6-email-archive/1.0"})
        try:
            with urllib.request.urlopen(r, timeout=180) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(3 * (i + 1)); continue
            raise SystemExit(f"GHL error HTTP {e.code} on {path.split('?')[0]}. Nothing written.")
        except urllib.error.URLError:
            time.sleep(3 * (i + 1))
    raise SystemExit(f"No answer from GHL for {path.split('?')[0]} after 6 tries. Nothing written.")


def export_once(start, end=None, contact=None):
    """All Email messages in [start, end), every page. Returns (messages, total GHL says exist)."""
    out, cur, total = [], None, None
    while True:
        p = {"locationId": LOC, "channel": "Email", "limit": 1000, "startDate": start, "sortOrder": "asc"}
        if end: p["endDate"] = end
        if contact: p["contactId"] = contact
        if cur: p["cursor"] = cur
        r = req("GET", "/conversations/messages/export", p)
        ms = r.get("messages", [])
        if total is None and isinstance(r.get("total"), int):
            total = r["total"]
        out += ms
        cur = r.get("nextCursor")
        if not cur or not ms:
            return out, total


def export(start, end=None, contact=None):
    """GHL sometimes returns an empty or short export. Retry until the count matches GHL's own total."""
    best = []
    for i in range(4):
        ms, total = export_once(start, end, contact)
        if len(ms) > len(best):
            best = ms
        if total is None or len(ms) >= total:
            return ms
        time.sleep(4 * (i + 1))
    return best


def probe_contacts():
    """Every ticket holder (tag event_warrior_con_6), in a fixed order. GHL's own sort isn't stable
    (many contacts share a dateAdded) and a contact's dateAdded isn't their purchase date, so a small
    sample can miss the early emails. Reading all holders (about 150 small reads) never misses one."""
    ids, page = [], 1
    while True:
        r = req("POST", "/contacts/search", body={"locationId": LOC, "pageLimit": 100, "page": page,
                "filters": [{"field": "tags", "operator": "eq", "value": TAG}]})
        cs = r.get("contacts", [])
        ids += [c["id"] for c in cs if "wakeupwarrior" not in (c.get("email") or "").lower()]
        if len(cs) < 100 or page >= MAX_PAGES:
            return sorted(set(ids))
        page += 1


def iso(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


# ------------------------------------------------------------------ cleaning
BLOCK = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "table", "blockquote", "ul", "ol", "center"}


class Paras(HTMLParser):
    """HTML -> list of paragraphs (each a list of lines). Links are tracked so they can be removed."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.paras, self.cur, self.skip, self.in_a, self.a_text = [], [""], 0, 0, ""
        self.link_paras = set()

    def flush(self):
        lines = [re.sub(r"[ \t ​‌‍﻿]+", " ", l).strip() for l in self.cur]
        lines = [l for l in lines if l]
        if lines:
            self.paras.append(lines)
        self.cur = [""]

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head", "title"): self.skip += 1
        elif tag == "br": self.cur.append("")
        elif tag in BLOCK: self.flush()
        elif tag == "a":
            self.in_a += 1; self.a_text = ""
            self.link_paras.add(len(self.paras))   # this paragraph had a link in it

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head", "title"): self.skip = max(0, self.skip - 1)
        elif tag in BLOCK: self.flush()
        elif tag == "a" and self.in_a:
            self.in_a -= 1
            # A link keeps only its words (the URL is never copied). If the visible words are themselves a URL,
            # they stay in the text on purpose: the safety check then skips the whole email instead of
            # publishing a sentence with a hole in it.

    def handle_data(self, d):
        if self.skip: return
        self.cur[-1] += d
        if self.in_a: self.a_text += d


GREETING = re.compile(r"^(hey|hi|hello|dear)?\s*[^\s,.!?]{0,30}(\s[^\s,.!?]{1,30})?\s*,$", re.I)
FOOTER = re.compile(r"unsubscribe|no longer wish to receive|manage (your )?preferences|update your preferences", re.I)
CTA = re.compile(r"\b(click here|join|log ?in|login|access|download|replay|get your|claim|register|watch now|reply)\b", re.I)


def clean(body_html):
    """Returns (paragraphs as strings, removed-items log). Raises ValueError if it can't clean safely."""
    p = Paras(); p.feed(body_html); p.flush()
    paras, removed = p.paras, []
    # 1) footer: cut at the first unsubscribe/preferences paragraph (and everything after it)
    for i, para in enumerate(paras):
        if FOOTER.search(" ".join(para)):
            removed.append(f"footer: {len(paras) - i} paragraph(s) from \"{' '.join(para)[:60]}\"")
            paras = paras[:i]; break
    if not paras:
        raise ValueError("no text left after removing the footer")
    # 2) greeting -> "Brother,"
    first = " ".join(paras[0])
    if not GREETING.match(first):
        raise ValueError("first line doesn't look like a greeting, so the personal name can't be removed safely")
    removed.append("greeting: personal first name -> \"Brother,\"")
    paras[0] = ["Brother,"]
    # 3) paragraphs that were only a link/button (a CTA line), e.g. "JOIN THE ROOM HERE"
    keep = []
    for i, para in enumerate(paras):
        txt = " ".join(para)
        if i in p.link_paras and i > 0 and len(txt) <= 80 and CTA.search(txt):
            removed.append(f"link/button paragraph: \"{txt[:60]}\""); continue
        keep.append("\n".join(para))
    return keep, removed


def unsafe_hits(text):
    return sorted(set(m.group(0).lower() for m in UNSAFE.finditer(text)))


# ------------------------------------------------------------------ main
def main():
    global TOK
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--raw-dir", action="append", default=[], help="folders with novNN_raw.txt to cross-check (local only)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--now", help="pretend it is this time (ISO), for tests")
    a = ap.parse_args()
    TOK = token()
    if not TOK:
        raise SystemExit("No GHL token (env GHL_TOKEN or Keychain ghl-wuw-kpi). Nothing written.")
    now = ts(a.now) if a.now else dt.datetime.now(dt.timezone.utc)

    try:
        arch = json.load(open(a.out))
    except FileNotFoundError:
        arch = {"emails": []}
    have = {e["subject_full"] for e in arch["emails"]}

    # 1) what did real ticket holders receive?
    probes = probe_contacts()
    if len(probes) < MIN_RECIPIENTS:
        raise SystemExit(f"Only {len(probes)} ticket-holder contacts found to check against. Nothing written.")
    since = SINCE
    if arch["emails"]:
        since = max(SINCE, iso(ts(max(e["sent_at"] for e in arch["emails"])) - dt.timedelta(days=2)))
    seen = {}   # subject -> list of send times
    for c in probes:
        for m in export_once(since, iso(now), contact=c)[0]:   # no retries here: ~150 holders overlap
            s = (m.get("subject") or "").strip()
            if m.get("direction") == "outbound" and s.startswith(PREFIX):
                seen.setdefault(s, []).append(ts(m["dateAdded"]))
    print(f"{len(probes)} ticket holders checked; {len(seen)} '(WC6) - ' subjects received; {len(have)} already in archive")

    raws = {}
    for d in a.raw_dir:
        for f in glob.glob(os.path.join(d, "nov*_raw.txt")):
            lines = [l.strip() for l in open(f, encoding="utf-8").read().split("\n") if l.strip()]
            raws[lines[0]] = (f, lines[1:])

    added, skipped = [], []
    for subj, times in sorted(seen.items(), key=lambda kv: max(kv[1])):
        if subj in have:
            continue
        # 2) count recipients around each distinct send time (latest first); test sends have 1-2 recipients
        result = None
        for t in sorted(set(t.replace(second=0, microsecond=0) for t in times), reverse=True):
            for attempt in range(3):   # a short/empty read must never turn into "not sent"
                win = [m for m in export(iso(t - dt.timedelta(hours=1)), iso(min(now, t + dt.timedelta(hours=3))))
                       if (m.get("subject") or "").strip() == subj and m.get("direction") == "outbound"]
                contacts = {m.get("contactId") for m in win}
                if len(contacts) >= MIN_RECIPIENTS:
                    break
                time.sleep(6 * (attempt + 1))
            if len(contacts) >= MIN_RECIPIENTS:
                result = (win, contacts); break
        if not result:
            print(f"  not sent yet (fewer than {MIN_RECIPIENTS} recipients): {subj}")
            continue
        win, contacts = result
        win.sort(key=lambda m: m["dateAdded"])
        sent_at = ts(win[0]["dateAdded"])
        median = ts(statistics.median_low([m["dateAdded"] for m in win]))
        # 3) clean two copies sent to different contacts; they must match
        try:
            copies, by = [], set()
            for m in win:
                if m.get("contactId") not in by and m.get("body"):
                    by.add(m.get("contactId")); copies.append(clean(m["body"]))
                if len(copies) == 2: break
            if len(copies) < 2:
                raise ValueError("couldn't read two copies of the email body")
            (body1, removed), (body2, _) = copies
            if body1 != body2:
                raise ValueError("two recipients' copies differ after cleaning (something personal may be left)")
            title = subj[len(PREFIX):].strip()
            text = "\n\n".join(body1)
            hits = unsafe_hits(title + "\n" + text)
            if hits:
                raise ValueError("safety check found " + ", ".join(f'"{h}"' for h in hits) + " left in the text")
            # 4) local cross-check against Garrett's source text, when available
            check = "no local source to compare"
            if subj in raws:
                f, lines = raws[subj]
                src = [l for l in lines[1:]]                           # drop the [NAME], line
                got = [l for para in body1[1:] for l in para.split("\n")]
                if src != got:
                    import difflib
                    diff = [x for x in difflib.unified_diff(src, got, lineterm="", n=0) if x[:1] in "+-" and x[:3] not in ("---", "+++")]
                    raise ValueError(f"words differ from the source {os.path.basename(f)}: {diff[:4]}")
                check = f"identical to {os.path.basename(f)} (after the greeting)"
        except ValueError as e:
            note(f"SKIPPED {sent_at.astimezone(ET):%b %d} \"{subj}\": {e}. Nothing published for this email.")
            skipped.append(subj); continue
        e = {"date": median.astimezone(ET).strftime("%Y-%m-%d"), "sent_at": iso(sent_at), "subject": title,
             "subject_full": subj, "body": text}
        added.append(e)
        print(f"  ADD {e['date']} \"{subj}\" ({len(contacts)} recipients; {check}; removed: {'; '.join(removed)})")

    emails = sorted(arch["emails"] + added, key=lambda e: e["sent_at"])
    for i, e in enumerate(emails, 1):
        e["n"] = i
    out = {"title": "Emails from Garrett (WarriorCon 6 ticket holders)", "count": len(emails),
           "updated_at": iso(now) if added else arch.get("updated_at", iso(now)),
           "emails": [{k: e[k] for k in ("n", "date", "sent_at", "subject", "subject_full", "body")} for e in emails]}
    # final whole-file safety check (never publish a bad file)
    for e in out["emails"]:
        if unsafe_hits(e["subject"] + "\n" + e["body"]):
            raise SystemExit(f"Final check failed on {e['subject']}. Nothing written.")
    print(f"{len(added)} added, {len(skipped)} skipped, {len(emails)} in archive")
    if not a.dry_run and (added or not os.path.exists(a.out)):
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
            f.write("\n")
        print("wrote", a.out)
    if NOTES:
        with open(os.environ.get("NOTES_FILE", "run-notes.md"), "a", encoding="utf-8") as f:
            f.write(f"## Run {iso(now)}\n" + "".join(f"- {n}\n" for n in NOTES))
    return 2 if skipped else 0


if __name__ == "__main__":
    sys.exit(main())
