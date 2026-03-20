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


def get_login_token():
    r = session.get(API_URL, params={
        "action": "query",
        "meta": "tokens",
        "type": "login",
        "format": "json"
    })
    return r.json()["query"]["tokens"]["logintoken"]


def verify_login(expected_username):
    r = session.get(API_URL, params={
        "action": "query",
        "meta": "userinfo",
        "format": "json"
    })
    data = r.json()
    
    # Debug
    logging.info(f"DEBUG: userinfo response: {data.get('query', {}).get('userinfo', {})}")
    
    if "query" not in data or "userinfo" not in data["query"]:
        return False
    userinfo = data["query"]["userinfo"]
    if userinfo.get("id", 0) == 0:
        return False
    actual_user = userinfo.get("name", "")
    return actual_user.lower() == expected_username.lower()


def login():
    username = os.getenv("WIKI_USERNAME")
    password = os.getenv("WIKI_PASSWORD")
    if not username or not password:
        raise Exception("Missing WIKI_USERNAME or WIKI_PASSWORD")

    token = get_login_token()
    r = session.post(API_URL, data={
        "action": "login",
        "lgname": username,
        "lgpassword": password,
        "lgtoken": token,
        "format": "json"
    })
    result = r.json()
    
    logging.info(f"DEBUG: login response: {result}")

    if result.get("login", {}).get("result") != "Success":
        raise Exception(f"Login failed: {result}")

    # Verify login
    if not verify_login(username):
        raise Exception("Login verification failed; not actually logged in")
    
    logging.info(f"Logged in as {username}")


def get_csrf_token():
    r = session.get(API_URL, params={
        "action": "query",
        "meta": "tokens",
        "format": "json"
    })
    return r.json()["query"]["tokens"]["csrftoken"]


def get_page(title):
    r = session.get(API_URL, params={
        "action": "query",
        "prop": "revisions",
        "rvprop": "content|ids",
        "titles": title,
        "format": "json",
        "maxlag": 5
    })
    pages = r.json()["query"]["pages"]
    page = next(iter(pages.values()))
    if "missing" in page:
        return None, None
    rev = page["revisions"][0]
    return rev["slots"]["main"]["*"], rev["revid"]


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


def normalize_ref(content):
    content = content.strip()
    content = re.sub(r"\s+", " ", content)
    return content


def is_inside_template(node):
    parent = node
    while parent:
        parent = getattr(parent, "parent", None)
        if parent and parent.__class__.__name__ == "Template":
            return True
    return False


def parse_worklist(text):
    items = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("*"):
            continue
        content = line[1:].strip()
        m = re.match(r"\[\[(.*?)\]\]", content)
        if m:
            items.append({"title": m.group(1)})
        else:
            items.append({"wikitext": content, "label": content})
    return items[:MAX_PAGES]


def fix_duplicate_refs(text):
    wikicode = mwparserfromhell.parse(text)
    refs = wikicode.filter_tags(matches=lambda n: n.tag == "ref")
    seen = {}
    count = 1
    changes = []

    for ref in refs:
        if ref.has("name"):
            continue
        if is_inside_template(ref):
            continue
        content = str(ref.contents).strip()
        norm = normalize_ref(content)
        if norm in seen:
            name = seen[norm]
            ref.attributes.append(("name", name))
            ref.contents = None
            changes.append((content, f'<ref name="{name}"/>'))
        else:
            name = f"auto{count}"
            seen[norm] = name
            ref.attributes.append(("name", name))
            changes.append((content, f'<ref name="{name}">{content}</ref>'))
            count += 1
    return str(wikicode), changes


def build_diff_link(new_rev):
    return f"https://test.wikipedia.org/?diff={new_rev}"


def update_list_page(original_text, results):
    text = original_text
    for item in results:
        article, summary = item["label"], item["summary"]
        pattern = re.escape(f"* {article}")
        replacement = f"<s>* {article}</s> – {summary}"
        text = re.sub(pattern, replacement, text, count=1)
    if DRY_RUN:
        print("\n--- LIST UPDATE ---\n")
        print(text[:1000])
        return
    edit_page(LIST_PAGE, text, "Updating processed articles (bot)")


def process_item(item):
    if "title" in item:
        text, old_rev = get_page(item["title"])
        label = item["title"]
    else:
        text = item["wikitext"]
        old_rev = None
        label = item.get("label", "Unnamed block")

    if not text or len(text) > MAX_SIZE:
        return None

    new_text, changes = fix_duplicate_refs(text)
    if new_text == text or not changes:
        return None

    if DRY_RUN:
        print(f"[DRY RUN] Would edit {label}")
        return {"label": label, "new_text": new_text, "changes": changes, "new_rev": None}

    if "title" in item:
        new_rev = edit_page(item["title"], new_text,
                            "Bot: Convert duplicate <ref> tags to named references (bot)")
        short_link = build_diff_link(new_rev)
    else:
        new_rev = None
        short_link = "RAW_WIKITEXT"  # no diff for raw blocks

    original, fixed = changes[0]
    summary_text = f"replaced <code><ref>{original}</ref></code> with <code>{fixed}</code>; {short_link}"

    return {"label": label, "summary": summary_text}


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
            time.sleep(5)  # polite delay
        except Exception as e:
            logging.error(f"Error on item {item}: {e}")

    if results:
        update_list_page(worklist_text, results)


if __name__ == "__main__":
    main()
