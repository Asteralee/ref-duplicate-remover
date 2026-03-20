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

    if result.get("login", {}).get("result") != "Success":
        raise Exception(f"Login failed: {result}")

    if not verify_login(username):
        raise Exception("Login appeared successful but verification failed (not actually logged in)")

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


def get_pages_from_list():
    text, _ = get_page(LIST_PAGE)
    if not text:
        return [], ""

    links = re.findall(r"\[\[(.*?)\]\]", text)

    pages = []
    for link in links:
        if f"<s>[[{link}]]</s>" not in text:
            pages.append(link)

    return pages[:MAX_PAGES], text


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


def build_diff_links(old_id, new_id):
    short = f"https://simple.wikipedia.org/?diff={new_id}"
    full = f"https://simple.wikipedia.org/w/index.php?diff={new_id}&oldid={old_id}"
    return short, full


def update_list_page(original_text, results):
    text = original_text

    for article, summary in results:
        pattern = re.escape(f"[[{article}]]")
        replacement = f"<s>[[{article}]]</s> – {summary}"
        text = re.sub(pattern, replacement, text, count=1)

    if DRY_RUN:
        print("\n--- LIST UPDATE ---\n")
        print(text[:1000])
        return

    edit_page(LIST_PAGE, text, "Bot: Updating processed articles")


def main():
    login()

    pages, list_text = get_pages_from_list()
    results = []

    for title in pages:
        try:
            text, old_rev = get_page(title)

            if not text or len(text) > MAX_SIZE:
                continue

            new_text, changes = fix_duplicate_refs(text)

            if new_text == text or not changes:
                continue

            if DRY_RUN:
                print(f"[DRY RUN] Would edit {title}")
                continue

            new_rev = edit_page(
                title,
                new_text,
                "Bot: Convert duplicate <ref> tags to named references"
            )

            short, full = build_diff_links(old_rev, new_rev)

            original, fixed = changes[0]

            summary = (
                f"replaced <code><ref>{original}</ref></code> "
                f"with <code>{fixed}</code>; "
                f"[{short} short] | [{full} full]"
            )

            results.append((title, summary))
            logging.info(f"Edited {title}")

            time.sleep(5)  # delay

        except Exception as e:
            logging.error(f"Error on {title}: {e}")

    if results:
        update_list_page(list_text, results)


if __name__ == "__main__":
    main()
