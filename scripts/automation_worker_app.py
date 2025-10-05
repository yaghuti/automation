#!/usr/bin/env python3
"""
automation_worker_app.py
- Reads the repository_dispatch payload (GITHUB_EVENT_PATH)
- Uses APP_PRIVATE_KEY and APP_ID (from repo secrets) to:
  1) build a JWT for the GitHub App
  2) list installations and find the installation for the target owner (client_payload.owner)
  3) exchange JWT for an installation token
  4) perform actions: upload_files, create_pr, update_file (dispatcher)

Requirements: requests, pyjwt[crypto]
This script runs inside the workflow above.
"""

import os, sys, time, json, base64
import requests
import jwt  # PyJWT

GITHUB_API = os.environ.get("GITHUB_API_URL", "https://api.github.com")
APP_ID = os.environ.get("APP_ID")
APP_PRIVATE_KEY = os.environ.get("APP_PRIVATE_KEY")  # PEM content

if not APP_ID or not APP_PRIVATE_KEY:
    print("ERROR: APP_ID and APP_PRIVATE_KEY must be set as repo secrets.")
    sys.exit(1)

EVENT_PATH = os.environ.get("GITHUB_EVENT_PATH")
if not EVENT_PATH or not os.path.exists(EVENT_PATH):
    print("ERROR: GITHUB_EVENT_PATH not found.")
    sys.exit(1)

with open(EVENT_PATH, "r", encoding="utf-8") as f:
    event = json.load(f)

payload = event.get("client_payload", {})
action = payload.get("action")

# Helper: create App JWT
def create_jwt(app_id, private_key_pem):
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + (9 * 60), "iss": int(app_id)}
    token = jwt.encode(payload, private_key_pem, algorithm="RS256")
    if isinstance(token, bytes):
        token = token.decode("utf-8")
    return token

# Helper: list installations and find installation id for owner (owner may be user or org)
def find_installation_id(jwt_token, owner_login):
    headers = {"Authorization": f"Bearer {jwt_token}", "Accept": "application/vnd.github+json"}
    url = f"{GITHUB_API}/app/installations"
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    installs = r.json()
    for inst in installs:
        acct = inst.get("account", {})
        if acct.get("login", "").lower() == owner_login.lower():
            return inst.get("id")
    return None

# Helper: create installation access token
def create_installation_token(jwt_token, installation_id):
    headers = {"Authorization": f"Bearer {jwt_token}", "Accept": "application/vnd.github+json"}
    url = f"{GITHUB_API}/app/installations/{installation_id}/access_tokens"
    r = requests.post(url, headers=headers)
    r.raise_for_status()
    return r.json().get("token")

# API helpers using installation token
def api_get(token, url):
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json", "User-Agent":"automation-worker"}
    return requests.get(url, headers=headers)

def api_put(token, url, json_body):
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json", "User-Agent":"automation-worker"}
    return requests.put(url, headers=headers, json=json_body)

def api_post(token, url, json_body):
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json", "User-Agent":"automation-worker"}
    return requests.post(url, headers=headers, json=json_body)

# Dispatcher actions
def upload_file(token, owner, repo, path, content, message="Add file via automation", branch=None):
    url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}"
    b64 = base64.b64encode(content.encode("utf-8")).decode("utf-8")
    body = {"message": message, "content": b64}
    if branch:
        body["branch"] = branch
    # try to GET to obtain sha if exists
    r_get = api_get(token, url)
    if r_get.status_code == 200:
        sha = r_get.json().get("sha")
        body["sha"] = sha
    r = api_put(token, url, body)
    return r

def create_pr(token, owner, repo, head, base="main", title="Automated PR", body_text=""):
    url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls"
    data = {"title": title, "head": head, "base": base, "body": body_text}
    return api_post(token, url, data)

# Main flow
def main():
    owner = payload.get("owner")  # required: owner login (user or org)
    repo = payload.get("repo")    # optional for some actions
    if not owner and not payload.get("owner_type"):
        print("ERROR: client_payload.owner is required to locate installation.")
        sys.exit(1)

    # create JWT
    jwt_token = create_jwt(APP_ID, APP_PRIVATE_KEY)

    # determine installation id: either secret INSTALLATION_ID provided, or find by owner
    installation_id_env = os.environ.get("INSTALLATION_ID")
    if installation_id_env:
        installation_id = installation_id_env
    else:
        installation_id = find_installation_id(jwt_token, owner)
        if not installation_id:
            print(f"ERROR: No installation found for owner '{owner}'. Ensure App is installed on that account/org.")
            sys.exit(1)

    # create installation token
    install_token = create_installation_token(jwt_token, installation_id)
    print("Got installation token (length):", len(install_token))

    # Dispatch actions
    if action == "upload_files":
        files = payload.get("files", [])
        if not repo or not files:
            print("ERROR: repo and files are required for upload_files")
            sys.exit(1)
        for f in files:
            path = f.get("path")
            content = f.get("content", "")
            message = f.get("message", "automation upload")
            r = upload_file(install_token, owner, repo, path, content, message=message, branch=f.get("branch"))
            print("upload_file", path, "status", r.status_code)
            if not r.ok:
                print("Response:", r.status_code, r.text)
                sys.exit(1)
        print("done upload_files")
    elif action == "create_pr":
        head = payload.get("head")
        base = payload.get("base", "main")
        title = payload.get("title", "Automated PR")
        body_text = payload.get("body", "")
        if not repo or not head:
            print("ERROR: repo and head are required for create_pr")
            sys.exit(1)
        r = create_pr(install_token, owner, repo, head, base=base, title=title, body_text=body_text)
        print("create_pr status", r.status_code, r.text)
        if not r.ok:
            sys.exit(1)
        print("done create_pr")
    else:
        print("Unknown or missing action in client_payload:", action)
        sys.exit(1)

if __name__ == "__main__":
    main()
