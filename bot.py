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
    "User-Agent": "RefDuplicateRemover/1.0"
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
        "rvprop": "content|ids",
        "rvslots": "main",
        "titles": title,
        "format": "json",
        "maxlag": 5
    })

    data = r.json()
    pages = data.get("query", {}).get("pages", {})
    page = next(iter(pages.values()), None)

    if not page or "missing" in page:
        return None, None

    revisions = page.get("revisions")
    if not revisions:
        return None, None

    rev = revisions[0]

    if "slots" in rev:
        text = rev["slots"]["main"].get("content", "")
    else:
        text = rev.get("*", "")

    return text, rev.get("revid")


def edit_page(title, text, summary):
    token = get_csrf_token()

    r = session.post(API_URL, data={
        "action": "edit",
        "title": title,
        "text": text,
        "summary": summary,
        "token": token,
        "format": "json",
        "maxlag": 5
    })

    result = r.json()
    if "error" in result:
        raise Exception(result["error"])

    return result["edit"]["newrevid"]


# ---------------- REF PROCESSING ----------------

def normalize_ref(content):
    return re.sub(r"\s+", " ", content.strip())


def is_inside_template(node):
    parent = node
    while parent:
        parent = getattr(parent, "parent", None)
        if parent and parent.__class__.__name__ == "Template":
            return True
    return False


def generate_ref_name(content, fallback_count):
    m = re.search(r"\|\s*(website|work|publisher)\s*=\s*([^|}]+)", content, re.I)
    if m:
        name = re.sub(r"[^a-z0-9]+", "-", m.group(2).lower())
        return name.strip("-")

    m = re.search(r"\|\s*title\s*=\s*([^|}]+)", content, re.I)
    if m:
        name = re.sub(r"[^a-z0-9]+", "-", m.group(1).lower())
        return name.strip("-")[:30]

    return f"auto{fallback_count}"


def fix_duplicate_refs(text):
    wikicode = mwparserfromhell.parse(text)
    refs = wikicode.filter_tags(matches=lambda n: n.tag == "ref")

    seen = {}
    count = 1
    changes = []

    for ref in refs:
        if is_inside_template(ref):
            continue

        content = str(ref.contents).strip() if ref.contents else ""
        norm = normalize_ref(content)

        # Learn from already named refs
        if ref.has("name"):
            name = str(ref.get("name").value).strip()
            if content:
                seen[norm] = name
            continue

        if not content:
            continue

        if norm in seen:
            name = seen[norm]
            ref.attributes["name"] = name
            ref.contents = ""
            ref.self_closing = True
            changes.append((content, f'<ref name="{name}"/>'))
        else:
            base = generate_ref_name(content, count)
            name = base
            i = 2

            while name in seen.values():
                name = f"{base}-{i}"
                i += 1

            seen[norm] = name
            ref.attributes["name"] = name
            changes.append((content, f'<ref name="{name}">{content}</ref>'))
            count += 1

    return str(wikicode), changes


# ---------------- WORKLIST ----------------

def parse_worklist(text):
    items = []

    for line in text.splitlines():
        line = line.strip()

        if not line.startswith("*"):
            continue

        content = line.lstrip("*").strip()

        m = re.match(r"\[\[\s*([^|\]]+)(?:\|[^\]]*)?\s*\]\]", content)
        if m:
            title = m.group(1).strip()
            items.append({"title": title, "label": content})
            continue

        if content and not any(x in content for x in ["{", "<", "[[File:", "[[Image:"]):
            items.append({"title": content, "label": content})
            continue

        items.append({"wikitext": content, "label": content})

    return items[:MAX_PAGES]


# ---------------- PROCESS ----------------

def build_diff_link(new_rev):
    return f"https://test.wikipedia.org/?diff={new_rev}"


def process_item(item):
    if "title" in item:
        text, _ = get_page(item["title"])
        label = item["title"]
    else:
        text = item["wikitext"]
        label = item.get("label", "Unnamed")

    if not text or len(text) > MAX_SIZE:
        return None

    new_text, changes = fix_duplicate_refs(text)

    if new_text == text or not changes:
        return None

    if DRY_RUN:
        print(f"[DRY RUN] {label}")
        return {"label": label}

    if "title" in item:
        new_rev = edit_page(
            item["title"],
            new_text,
            "Bot: Fix duplicate references"
        )
        link = build_diff_link(new_rev)
    else:
        link = "RAW"

    original, fixed = changes[0]
    summary = f"Replaced <ref>{original}</ref> with {fixed}; {link}"

    return {"label": label, "summary": summary}


def update_list_page(original_text, results):
    text = original_text

    for item in results:
        pattern = re.escape(f"* {item['label']}")
        replacement = f"<s>* {item['label']}</s> – {item['summary']}"
        text = re.sub(pattern, replacement, text, count=1)

    if DRY_RUN:
        print(text[:1000])
        return

    edit_page(LIST_PAGE, text, "Updating processed articles (bot)")


# ---------------- MAIN ----------------

def main():
    login()

    worklist_text, _ = get_page(LIST_PAGE)
    items = parse_worklist(worklist_text)

    results = []

    for item in items:
        try:
            result = process_item(item)
            if result:
                results.append(result)
            time.sleep(5)
        except Exception as e:
            logging.error(f"Error on item {item}: {e}")

    if results:
        update_list_page(worklist_text, results)


if __name__ == "__main__":
    main()
