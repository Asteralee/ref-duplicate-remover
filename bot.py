import os
import re
import time
import requests
import logging
import mwparserfromhell

API_URL = "https://test.wikipedia.org/w/api.php"
LIST_PAGE = "User:AsteraBot/Pages to fix"
MAX_PAGES = 10
MAX_SIZE = 200_000
DRY_RUN = False

session = requests.Session()
session.headers.update({
    "User-Agent": "RefDuplicateRemover/4.0"
})

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")


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


# ---------------- PAGE FETCH ----------------

def get_csrf_token():
    r = session.get(API_URL, params={
        "action": "query", "meta": "tokens", "format": "json"
    })
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

    if "slots" in rev:
        text = rev["slots"]["main"].get("*", "")
    else:
        text = rev.get("*", "")

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


# ---------------- REF PROCESSING ----------------

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
    url = url.rstrip("/")
    return url

def generate_smart_name(content, fallback_count):
    for field in ["website", "publisher", "work"]:
        val = extract_field(content, field)
        if val:
            val = re.sub(r"https?://(www\.)?", "", val.lower())
            val = val.split("/")[0]
            name = re.sub(r"[^a-z0-9]+", "-", val)
            return name.strip("-")[:30]

    title = extract_field(content, "title")
    if title:
        name = re.sub(r"[^a-z0-9]+", "-", title.lower())
        return name.strip("-")[:30]

    return f"ref{fallback_count}"

def is_inside_template(node):
    parent = getattr(node, "parent", None)
    while parent:
        if parent.__class__.__name__ == "Template":
            return True
        parent = getattr(parent, "parent", None)
    return False

def fix_duplicate_refs(text):
    logging.info("Running duplicate ref fixer")
    wikicode = mwparserfromhell.parse(text)
    refs = [tag for tag in wikicode.filter_tags() if str(tag.tag).lower() == "ref"]
    logging.info(f"Ref tags found: {len(refs)}")

    existing_names = set()
    for ref in refs:
        if ref.has("name"):
            existing_names.add(str(ref.get("name").value).strip())

    seen = {}
    name_used = set(existing_names)
    fallback_count = 1
    changes = []

    for ref in refs:
        content = str(ref.contents).strip() if ref.contents else ""
        if not content:
            continue

        # ---------------- TEMPLATE-SAFE IMPROVEMENT ----------------
        if is_inside_template(ref):
            continue
        if re.search(r"\{\{\s*(harv|sfn|sfnp)", content, re.I):
            continue
        # ----------------------------------------------------------

        url = extract_url(content)
        norm_url = normalize_url(url)
        key = norm_url if norm_url else normalize_ref(content)
        existing_name = str(ref.get("name").value).strip() if ref.has("name") else None

        if key not in seen:
            if existing_name:
                name = existing_name
            else:
                name = generate_smart_name(content, fallback_count)
            base = name
            i = 2
            while name in name_used:
                name = f"{base}-{i}"
                i += 1
            seen[key] = name
            name_used.add(name)
            if not ref.has("name"):
                ref.add("name", name)
            fallback_count += 1
        else:
            name = seen[key]
            new_tag = f'<ref name="{name}"/>'
            wikicode.replace(ref, new_tag)
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
        replacement = f"<s>* [[{item['label']}]]</s> – done"
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
    logging.info(f"Worklist preview:\n{worklist_text[:300]}")
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
