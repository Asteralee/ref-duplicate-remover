import os
import re
import time
import requests
import logging
import mwparserfromhell
from difflib import SequenceMatcher

# ---------------- CONFIG ----------------
API_URL = "https://test.wikipedia.org/w/api.php"
LIST_PAGE = "User:AsteraBot/pages to fix"
MAX_PAGES = 10
MAX_SIZE = 200_000
DRY_RUN = False  

session = requests.Session()
session.headers.update({"User-Agent": "RefDuplicateRemover/5.0"})

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")

# ---------------- HELPERS ----------------
def similarity(a, b):
    return SequenceMatcher(None, a, b).ratio()

def normalize_ref(content):
    content = re.sub(r"\s+", " ", content.strip())
    content = re.sub(r"\|\s+", "|", content)
    content = re.sub(r"\s+\|", "|", content)
    return content.lower()

def extract_field(content, field):
    m = re.search(rf"\|\s*{field}\s*=\s*([^|}}]+)", content, re.I)
    return m.group(1).strip() if m else None

def extract_url(content):
    return extract_field(content, "url")

def normalize_url(url):
    if not url:
        return None
    url = url.strip().lower()
    url = re.sub(r"^https?://", "", url)
    url = re.sub(r"^www\.", "", url)
    url = re.sub(r"\?.*$", "", url)
    return url.rstrip("/")

# ---------------- LOGIN ----------------
def get_login_token():
    r = session.get(API_URL, params={
        "action": "query", "meta": "tokens", "type": "login", "format": "json"
    })
    return r.json()["query"]["tokens"]["logintoken"]

def verify_login(expected_username):
    r = session.get(API_URL, params={
        "action": "query", "meta": "userinfo", "format": "json"
    })
    userinfo = r.json().get('query', {}).get('userinfo', {})
    return userinfo.get("name", "").lower() == expected_username.lower()

def login():
    username = os.getenv("WIKI_USERNAME")
    password = os.getenv("WIKI_PASSWORD")
    if not username or not password:
        raise Exception("Missing credentials")

    token = get_login_token()
    r = session.post(API_URL, data={
        "action": "login",
        "lgname": username,
        "lgpassword": password,
        "lgtoken": token,
        "format": "json"
    })
    if r.json().get("login", {}).get("result") != "Success":
        raise Exception("Login failed")
    if not verify_login(username):
        raise Exception("Login verification failed")
    logging.info(f"Logged in as {username}")

# ---------------- PAGE FETCH & EDIT ----------------
def get_csrf_token():
    r = session.get(API_URL, params={"action": "query", "meta": "tokens", "format": "json"})
    return r.json()["query"]["tokens"]["csrftoken"]

def get_page(title):
    r = session.get(API_URL, params={
        "action": "query",
        "prop": "revisions",
        "rvprop": "ids|content",
        "rvslots": "main",
        "titles": title,
        "format": "json",
        "maxlag": 5
    })
    data = r.json()
    pages = data.get("query", {}).get("pages", {})
    page = next(iter(pages.values()), None)
    if not page or "missing" in page:
        logging.warning(f"Page missing: {title}")
        return None, None
    rev = page.get("revisions", [{}])[0]
    text = rev.get("slots", {}).get("main", {}).get("*") if "slots" in rev else rev.get("*", "")
    if not text:
        logging.warning(f"No content retrieved for {title}")
    return text, rev.get("revid")

def edit_page(title, text, summary, base_revid):
    token = get_csrf_token()
    for attempt in range(3):
        r = session.post(API_URL, data={
            "action": "edit",
            "title": title,
            "text": text,
            "summary": summary,
            "token": token,
            "format": "json",
            "baserevid": base_revid,
            "maxlag": 5
        })
        result = r.json()
        if "error" in result:
            if result["error"].get("code") == "maxlag":
                logging.warning("Maxlag hit, retrying...")
                time.sleep(5)
                continue
            raise Exception(result["error"])
        return result["edit"]["newrevid"]
    raise Exception("Edit failed after retries")

# ---------------- CITATION LOGIC ----------------
def parse_cite_template(content):
    wikicode = mwparserfromhell.parse(content)
    templates = wikicode.filter_templates()
    for t in templates:
        name = str(t.name).strip().lower()
        if any(x in name for x in ["cite web", "cite news", "cite journal", "cite book"]):
            data = {str(p.name).strip().lower(): str(p.value).strip() for p in t.params}
            return data
    return None

def get_canonical_url(cite_data):
    archive = normalize_url(cite_data.get("archive-url")) if cite_data else None
    original = normalize_url(cite_data.get("url")) if cite_data else None
    return archive or original

def normalize_field(val):
    if not val:
        return None
    val = val.lower().strip()
    val = re.sub(r"\s+", " ", val)
    return val

def cite_templates_match(a, b):
    if not a or not b:
        return False
    url_a = get_canonical_url(a)
    url_b = get_canonical_url(b)
    if not url_a or not url_b or url_a != url_b:
        return False
    fields = ["title", "publisher", "website", "work"]
    matches = 0
    total = 0
    for f in fields:
        va, vb = normalize_field(a.get(f)), normalize_field(b.get(f))
        if va and vb:
            total += 1
            if similarity(va, vb) >= 0.9:
                matches += 1
    return matches >= max(1, total - 1)

def generate_human_name(data, fallback_count):
    if not data:
        return f"ref{fallback_count}"
    url = get_canonical_url(data)
    domain = url.split("/")[0] if url else None
    date = data.get("date", "")
    year_match = re.search(r"\b(19|20)\d{2}\b", date)
    year = year_match.group(0) if year_match else None
    title = data.get("title", "")
    keyword = None
    if title:
        words = re.findall(r"[a-zA-Z0-9]+", title.lower())
        if words:
            keyword = words[0]
    parts = [p for p in [domain, year, keyword] if p]
    if parts:
        name = "-".join(parts)
        return re.sub(r"[^a-z0-9\-]", "", name)[:40]
    return f"ref{fallback_count}"

def fix_duplicate_refs(text):
    logging.info("Running advanced duplicate ref fixer")
    wikicode = mwparserfromhell.parse(text)
    refs = [tag for tag in wikicode.filter_tags() if str(tag.tag).lower() == "ref"]
    seen = []  # list of dicts: {name, content, parsed}
    name_used = set()
    fallback_count = 1
    changes = []
    for ref in refs:
        if not ref.contents:
            continue
        content = str(ref.contents).strip()
        if re.search(r"\{\{\s*(harv|sfn|sfnp)", content, re.I):
            continue
        parsed = parse_cite_template(content)
        norm_content = normalize_ref(content)
        existing_name = str(ref.get("name").value).strip() if ref.has("name") else None
        match = None
        for item in seen:
            if norm_content == item["content"]:
                match = item
                break
            if parsed and item["parsed"] and cite_templates_match(parsed, item["parsed"]):
                match = item
                break
        if not match:
            name = existing_name or generate_human_name(parsed, fallback_count)
            base = name
            i = 2
            while name in name_used:
                name = f"{base}-{i}"
                i += 1
            seen.append({"name": name, "content": norm_content, "parsed": parsed})
            name_used.add(name)
            if not ref.has("name"):
                ref.add("name", name)
            fallback_count += 1
        else:
            name = match["name"]
            new_node = mwparserfromhell.parse(f'<ref name="{name}"/>').nodes[0]
            wikicode.replace(ref, new_node)
            changes.append(name)
    logging.info(f"Duplicate refs fixed: {len(changes)}")
    return str(wikicode), changes

# ---------------- WORKLIST ----------------
def parse_worklist(text):
    items = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(("*", "#")):
            continue
        content = line.lstrip("*# ").strip()
        m = re.match(r"\[\[\s*([^|\]]+)", content)
        if m:
            items.append({"title": m.group(1).strip(), "label": content})
    logging.info(f"Parsed {len(items)} worklist items")
    return items[:MAX_PAGES]

# ---------------- PROCESS ----------------
def process_item(item):
    label = item["title"]
    logging.info(f"Processing: {label}")
    text, revid = get_page(label)
    if not text or len(text) > MAX_SIZE:
        logging.warning(f"Skipping {label}: no content or too large")
        return None
    new_text, changes = fix_duplicate_refs(text)
    if not changes:
        logging.info(f"No duplicate refs in {label}")
        return None
    if DRY_RUN:
        logging.info(f"[DRY RUN] Would edit {label}")
        return {"label": label}
    new_rev = edit_page(label, new_text, "Bot: Fix duplicate references", revid)
    return {"label": label, "rev": new_rev}

def update_list_page(original_text, results):
    text = original_text
    for item in results:
        pattern = re.escape(f"* [[{item['label']}]]")
        replacement = f"* <s>[[{item['label']}]]</s> – done"
        text = re.sub(pattern, replacement, text, count=1)
    if DRY_RUN:
        return
    edit_page(LIST_PAGE, text, "Updating processed articles (bot)", None)

# ---------------- MAIN ----------------
def main():
    login()
    worklist_text, _ = get_page(LIST_PAGE)
    if not worklist_text:
        logging.error("Worklist page empty!")
        return
    items = parse_worklist(worklist_text)
    results = []
    for item in items:
        try:
            result = process_item(item)
            if result:
                results.append(result)
            time.sleep(3)
        except Exception as e:
            logging.error(f"Error on {item}: {e}")
    logging.info(f"Total edits: {len(results)}")
    if results:
        update_list_page(worklist_text, results)

if __name__ == "__main__":
    main()
