"""docbot-all: writes docs for every repo you own into docs/<repo>.md and keeps an index in README.md."""
import datetime
import io
import json
import os
import re
import sys
import tarfile
import time

import requests
from google import genai

API = "https://api.github.com"
HEAD = {
    "Authorization": f"Bearer {os.environ['GH_PAT']}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
INCLUDE_PRIVATE = os.environ.get("INCLUDE_PRIVATE", "false").lower() == "true"
MAX_PER_RUN = int(os.environ.get("MAX_REPOS_PER_RUN", "10"))
SELF = os.environ.get("GITHUB_REPOSITORY", "").split("/")[-1]
TODAY = datetime.datetime.now(datetime.timezone.utc).date().isoformat()

STATE_FILE = "state.json"
NOTES_FILE = "DOCS_NOTES.md"  # optional file inside a project repo: goals, costs, audience
MAX_FILE_CHARS = 30_000
MAX_TOTAL_CHARS = 300_000
MAX_TARBALL_BYTES = 100_000_000
MODELS = ["gemini-flash-latest", "gemini-flash-lite-latest"]

SKIP_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".pdf", ".zip", ".gz",
    ".mp4", ".mov", ".mkv", ".webm", ".mp3", ".wav", ".woff", ".woff2", ".ttf",
    ".lock", ".min.js", ".map", ".pyc",
}
SKIP_DIRS = {"node_modules", "venv", ".venv", "dist", "build", "__pycache__", ".git"}
# Never send anything that looks like a secret to the AI.
SECRET_HINTS = ("cookie", ".env", "secret", "token", "credential", "id_rsa", ".pem", ".key")

PROMPT = """You are a technical writer. Write a clear, well-structured project document in Markdown \
for the repository "{repo}", based ONLY on the files below.

Rules:
- Use only facts found in the files. Do not invent features, costs or numbers. If something is \
not in the files, write "Not specified" or leave that point out.
- Audience: semi-technical clients and recruiters, plus the owner/operator.
- Use headings, short paragraphs, tables and bullet lists. No emoji.
- Include exactly one Mermaid flowchart of the full workflow (```mermaid block).
- Include one table of all tools and technologies (tool, what it is, role in this project, cost).
- Never output secret values. Secret names are fine.
- Output only the Markdown document, no commentary and no wrapping code fence.

Structure:
1. Title + one-line pitch
2. At a glance (small table: inputs, outputs, time, cost, platforms)
3. Problem it solves
4. What it does (features)
5. Full workflow, step by step (with Mermaid flowchart)
6. Architecture
7. Tools and technologies (table)
8. Inputs and outputs
9. Reliability and error handling
10. Security
11. Setup and configuration
12. Usage guide
13. Maintenance tasks
14. Limitations
15. Costs
16. Possible future improvements (only if clearly implied by the files)

FILES:
{files}
"""


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"last_run": "", "repos": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def list_repos():
    repos, page = [], 1
    while True:
        r = requests.get(
            f"{API}/user/repos",
            headers=HEAD,
            params={"affiliation": "owner", "per_page": 100, "page": page},
            timeout=60,
        )
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        repos += batch
        page += 1
    out = []
    for x in repos:
        if x["fork"] or x["archived"] or x["name"] == SELF:
            continue
        if x["private"] and not INCLUDE_PRIVATE:
            continue
        out.append(x)
    return out


def head_sha(full, branch):
    r = requests.get(f"{API}/repos/{full}/commits/{branch}", headers=HEAD, timeout=60)
    if r.status_code in (404, 409):  # empty repo or missing branch
        return None
    r.raise_for_status()
    return r.json()["sha"]


def wanted(path):
    low = path.lower()
    if any(part in SKIP_DIRS for part in path.split("/")):
        return False
    if any(low.endswith(ext) for ext in SKIP_EXT):
        return False
    if any(h in low for h in SECRET_HINTS):
        return False
    return True


def priority(path):
    low = path.lower()
    if low == NOTES_FILE.lower():
        return 0
    if low.startswith("readme"):
        return 1
    if low.startswith(".github/workflows/"):
        return 2
    return 3


def fetch_files(full, sha):
    r = requests.get(f"{API}/repos/{full}/tarball/{sha}", headers=HEAD, timeout=180)
    r.raise_for_status()
    if len(r.content) > MAX_TARBALL_BYTES:
        raise RuntimeError("repo archive too large")
    files = {}
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tar:
        for m in tar.getmembers():
            if not m.isfile() or m.size > 1_000_000:
                continue
            path = m.name.split("/", 1)[1] if "/" in m.name else m.name
            if not wanted(path):
                continue
            try:
                text = tar.extractfile(m).read().decode("utf-8")
            except (UnicodeDecodeError, AttributeError):
                continue
            if text.strip():
                files[path] = text
    return files


def build_corpus(files):
    parts, total = [], 0
    for p in sorted(files, key=lambda p: (priority(p), len(p))):
        text = files[p]
        if len(text) > MAX_FILE_CHARS:
            text = text[:MAX_FILE_CHARS] + "\n[... truncated ...]"
        block = f"\n===== FILE: {p} =====\n{text}\n"
        if total + len(block) > MAX_TOTAL_CHARS:
            break
        parts.append(block)
        total += len(block)
    return "".join(parts)


def generate(prompt):
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    last_err = None
    for model in MODELS:
        for _ in range(2):
            try:
                resp = client.models.generate_content(model=model, contents=prompt)
                text = (resp.text or "").strip()
                if len(text) > 500:
                    return text
                last_err = f"{model}: output too short"
            except Exception as e:  # network, quota, bad key, etc.
                last_err = f"{model}: {e}"
            print(f"Attempt failed -> {last_err}", file=sys.stderr)
            time.sleep(15)
    raise RuntimeError(f"All Gemini attempts failed. Last error: {last_err}")


def clean(text):
    m = re.match(r"^```(?:markdown|md)?\s*\n(.*)\n```\s*$", text, re.DOTALL)
    return (m.group(1) if m else text).strip() + "\n"


def write_index(state):
    rows = sorted(state["repos"].items(), key=lambda kv: kv[1].get("updated", ""), reverse=True)
    lines = [
        "# Product documentation",
        "",
        "Auto-generated documentation for every repository. Updated by the docbot workflow.",
        "",
        "| Product | Description | Last updated |",
        "|---|---|---|",
    ]
    for name, info in rows:
        desc = (info.get("description") or "").replace("|", "/")
        lines.append(f"| [{name}](docs/{name}.md) | {desc} | {info.get('updated', '')} |")
    with open("README.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main():
    os.makedirs("docs", exist_ok=True)
    state = load_state()
    done = 0
    for r in list_repos():
        if done >= MAX_PER_RUN:
            print("Per-run limit reached; remaining repos will be done next run.")
            break
        name, full, branch = r["name"], r["full_name"], r["default_branch"]
        try:
            sha = head_sha(full, branch)
            if not sha or state["repos"].get(name, {}).get("sha") == sha:
                continue
            corpus = build_corpus(fetch_files(full, sha))
            if not corpus:
                continue
            doc = clean(generate(PROMPT.format(repo=full, files=corpus)))
        except Exception as e:
            print(f"Skipping {full}: {e}", file=sys.stderr)
            continue  # state not updated, so it is retried next run
        header = f"> Auto-generated from [{full}]({r['html_url']}) at commit `{sha[:7]}` on {TODAY}.\n\n"
        with open(f"docs/{name}.md", "w", encoding="utf-8") as f:
            f.write(header + doc)
        state["repos"][name] = {
            "sha": sha,
            "updated": TODAY,
            "url": r["html_url"],
            "description": r.get("description") or "",
        }
        done += 1
        print(f"Documented {full}")
        time.sleep(5)
    state["last_run"] = TODAY  # changes daily, which keeps the scheduled workflow from being paused
    save_state(state)
    write_index(state)


if __name__ == "__main__":
    main()
