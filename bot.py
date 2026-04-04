import os
import re
import time
import requests
import mwparserfromhell
from difflib import SequenceMatcher
import datetime

API_URL = "https://test.wikipedia.org/w/api.php"
LIST_PAGE = "User:AsteraBot/pages to fix"
MAX_PAGES = 10
MAX_SIZE = 200_000
DRY_RUN = False

session = requests.Session()
session.headers.update({"User-Agent": "RefDuplicateRemover/8.0"})


def similarity(a, b):
    return SequenceMatcher(None, a, b).ratio()


def normalize_ref(content):
    content = re.sub(r"\s+", " ", content.strip())
    content = re.sub(r"\|\s+", "|", content)
    content = re.sub(r"\s+\|", "|", content)
    return content.lower()


def normalize_field(val):
    if not val:
        return None
    val = val.lower().strip()
    val = re.sub(r"\s+", " ", val)
    return val


def normalize_url(url):
    if not url:
        return None
    url = url.strip().lower()
    url = re.sub(r"^https?://", "", url)
    url = re.sub(r"^www\.", "", url)
    url = re.sub(r"\?.*$", "", url)
    url = re.sub(r"#.*$", "", url)  # NEW
    url = url.rstrip("/")
    url = url.replace("/amp", "")  # optional cleanup
    return url


def get_login_token():
    r = session.get(API_URL, params={"action": "query", "meta": "tokens", "type": "login", "format": "json"})
    return r.json()["query"]["tokens"]["logintoken"]


def verify_login(expected_username):
    r = session.get(API_URL, params={"action": "query", "meta": "userinfo", "format": "json"})
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

    print(f"Logged in as {username}")


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

    pages = r.json().get("query", {}).get("pages", {})
    page = next(iter(pages.values()), None)

    if not page or "missing" in page:
        print(f"Page missing: {title}")
        return None, None

    rev = page.get("revisions", [{}])[0]
    text = rev.get("slots", {}).get("main", {}).get("*") if "slots" in rev else rev.get("*", "")

    return text, rev.get("revid")


def edit_page(title, text, summary, base_revid):
    token = get_csrf_token()

    for _ in range(3):
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
                time.sleep(5)
                continue
            raise Exception(result["error"])

        return result["edit"]["newrevid"]

    raise Exception("Edit failed")


def parse_cite_template(content):
    wikicode = mwparserfromhell.parse(content)
    for t in wikicode.filter_templates():
        name = str(t.name).strip().lower()
        if any(x in name for x in ["cite web", "cite news", "cite journal", "cite book"]):
            return {str(p.name).strip().lower(): str(p.value).strip() for p in t.params}
    return None


def get_canonical_url(cite_data):
    if not cite_data:
        return None
    archive = normalize_url(cite_data.get("archive-url"))
    original = normalize_url(cite_data.get("url"))
    return archive or original


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
            if similarity(va, vb) >= 0.85:  # slightly relaxed
                matches += 1

    return matches >= max(1, total - 2)


def merge_similar_groups(key_map, parsed_map):
    keys = list(key_map.keys())
    merged = {}
    used = set()

    for i, k1 in enumerate(keys):
        if k1 in used:
            continue

        merged[k1] = list(key_map[k1])
        used.add(k1)

        for k2 in keys[i + 1:]:
            if k2 in used:
                continue

            if cite_templates_match(parsed_map.get(k1), parsed_map.get(k2)):
                merged[k1].extend(key_map[k2])
                used.add(k2)

    return merged


def generate_human_name(data, fallback_count):
    if not data:
        return f"ref{fallback_count}"

    url = get_canonical_url(data)
    domain = url.split("/")[0] if url else None

    title = data.get("title", "")
    keyword = None
    if title:
        words = re.findall(r"[a-zA-Z0-9]+", title.lower())
        if words:
            keyword = words[0]

    parts = [p for p in [domain, keyword] if p]
    if parts:
        return re.sub(r"[^a-z0-9\-]", "", "-".join(parts))[:40]

    return f"ref{fallback_count}"


def fix_duplicate_refs(text):
    wikicode = mwparserfromhell.parse(text)
    refs = [tag for tag in wikicode.filter_tags() if str(tag.tag).lower() == "ref"]

    key_map = {}
    parsed_map = {}

    for ref in refs:
        if not ref.contents:
            continue

        content = str(ref.contents).strip()

        if re.search(r"\{\{\s*(harv|sfn|sfnp)", content, re.I):
            continue

        parsed = parse_cite_template(content)
        url = get_canonical_url(parsed)

        if url:
            key = url
            key_map.setdefault(key, []).append(ref)
            parsed_map[key] = parsed
        else:
            norm = normalize_ref(content)
            matched = False

            for existing in key_map:
                if similarity(existing, norm) > 0.92:
                    key_map[existing].append(ref)
                    matched = True
                    break

            if not matched:
                key_map[norm] = [ref]
                parsed_map[norm] = parsed

    # 🔥 second-pass merge
    key_map = merge_similar_groups(key_map, parsed_map)

    name_used = set()
    fallback_count = 1
    changes = []

    for key, ref_list in key_map.items():
        if len(ref_list) < 2:
            continue

        first_ref = ref_list[0]
        parsed = parsed_map.get(key)

        if first_ref.has("name"):
            name = str(first_ref.get("name").value).strip()
        else:
            name = generate_human_name(parsed, fallback_count)
            base = name
            i = 2
            while name in name_used:
                name = f"{base}-{i}"
                i += 1
            first_ref.add("name", name)
            fallback_count += 1

        name_used.add(name)

        for dup_ref in ref_list[1:]:
            new_node = mwparserfromhell.parse(f'<ref name="{name}"/>').nodes[0]
            wikicode.replace(dup_ref, new_node)
            changes.append(name)

    return str(wikicode), changes


# --- rest unchanged ---

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
    return items[:MAX_PAGES]


def process_item(item):
    label = item["title"]
    print(f"Processing: {label}")
    text, revid = get_page(label)

    if not text or len(text) > MAX_SIZE:
        return None

    new_text, changes = fix_duplicate_refs(text)

    if not changes:
        print(f"No duplicate refs in {label}")
        return None

    if DRY_RUN:
        print(f"[DRY RUN] Would edit {label}")
        return {"label": label}

    edit_page(label, new_text, "Bot: Fix duplicate references", revid)
    return {"label": label, "rev": revid}


def update_list_page(original_text, results):
    text = original_text
    today = datetime.date.today().strftime("%d %B %Y")

    for item in results:
        pattern = re.escape(f"* [[{item['label']}]]")
        replacement = f"* [[{item['label']}]] {{{{done}}}} on {today}"
        text = re.sub(pattern, replacement, text, count=1)

    if not DRY_RUN:
        edit_page(LIST_PAGE, text, "Updating processed articles (bot)", None)


def main():
    login()
    worklist_text, _ = get_page(LIST_PAGE)

    if not worklist_text:
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
            print(f"Error: {e}")

    if results:
        update_list_page(worklist_text, results)


if __name__ == "__main__":
    main()
