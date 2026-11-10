#!/usr/bin/env python3
"""kakapo-feeds (PUBLIC repo): fail if a secret value or a secret-looking string is in the repository files or in dist/.

The repository is PUBLIC. In CI the secret VALUES come from the environment (GitHub secrets); locally from
~/ai/credentials/. Values are never printed — only the file and the secret's NAME.

    python3 scripts/verify_no_secrets.py           # repo tracked files + dist/
    python3 scripts/verify_no_secrets.py --repo    # only files git would commit
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
SECRET_NAMES = ["CEDOK_SFTP_HOST", "CEDOK_SFTP_USER", "CEDOK_SFTP_PASS", "ATIS_FEED_URL", "KAKAPO_IMPORT_URL",
                "KAKAPO_IMPORT_SECRET", "CF_API_KEY", "CF_GLOBAL_API_KEY"]
# public by design: CJ_PID / CID (in every affiliate link / not usable without the key), IndexNow key (served as a file)
PATTERNS = [
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "private key"),
    (r"\bAKIA[0-9A-Z]{16}\b", "AWS key"),
    (r"\bgh[pousr]_[A-Za-z0-9]{30,}\b", "GitHub token"),
    (r"\bxox[abpr]-[A-Za-z0-9-]{10,}", "Slack token"),
    (r"(?i)\b(api[_-]?key|apikey|secret|password|passwd|token)\b\s*[:=]\s*['\"][A-Za-z0-9_\-/+=]{16,}['\"]", "hard-coded credential"),
    (r"(?i)[?&](apiKey|api-key|token|access_token|hash)=[A-Za-z0-9_\-]{16,}", "credential in URL"),
]
ALLOW_URL = re.compile(r"(anrdoezrs\.net|jdoqocy\.com|tkqlhce\.com|dpbolvw\.net|kqzyfj\.com|ehub\.cz/system/scripts/click\.php|go\.dognet\.com)")


def secret_values():
    vals = {}
    for n in SECRET_NAMES:
        v = os.environ.get(n, "")
        if len(v) >= 10:
            vals[n] = v
    cred = Path(os.environ.get("KAKAPO_CRED_DIR", "/home/kenny/ai/credentials"))
    if not os.environ.get("GITHUB_ACTIONS") and cred.exists():
        for f in cred.glob(".*"):
            if f.is_file() and f.suffix in (".env", ".json", ".secret"):
                try:
                    text = f.read_text()
                except (UnicodeDecodeError, PermissionError):
                    continue
                for line in text.splitlines():
                    m = re.match(r"^([A-Z0-9_]+)=(.+)$", line.strip())
                    if m and m.group(1) not in ("SITE_URL", "CJ_PID", "CJ_CID", "INDEXNOW_KEY", "CF_ACCOUNT_ID", "CF_ZONE_ID", "CF_WORKERS_SUBDOMAIN",
                                                "WP_URL", "WP_USER", "FTP_HOST", "FTP_PORT", "FTP_USER", "CF_EMAIL", "EHUB_PUBLISHER_ID", "CJ_WEBSITE_ID",
                                                "DOGNET_EMAIL", "DOGNET_CHANNEL_CODE", "GA4_ID", "CEDOK_SFTP_FILE", "FTP_ROOT", "WORKERS_SUBDOMAIN") and not m.group(1).endswith("_WP_URL") and len(m.group(2).strip("\"' ")) >= 12:
                        vals[f"{f.name}:{m.group(1)}"] = m.group(2).strip("\"' ")
                if f.suffix == ".json":
                    try:
                        for k, v in json.loads(text).items():
                            if isinstance(v, str) and len(v) > 20:
                                vals[f"{f.name}:{k}"] = v
                    except Exception:
                        pass
    feeds = os.environ.get("FEEDS_JSON")
    if feeds:
        try:
            for k, v in json.loads(feeds).items():
                vals[f"FEEDS_JSON:{k}"] = v
        except ValueError:
            pass
    return vals


def files(repo_only):
    out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=APP, capture_output=True, text=True).stdout.split()
    fs = [APP / f for f in out]
    if not repo_only and (APP / "dist").exists():
        fs += [p for p in (APP / "dist").rglob("*") if p.is_file() and p.suffix in (".html", ".json", ".txt", ".js", ".css", ".xml", "")]
    return fs


def main():
    repo_only = "--repo" in sys.argv
    vals = secret_values()
    bad = []
    for f in files(repo_only):
        try:
            text = f.read_text()
        except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
            continue
        rel = f.relative_to(APP)
        for name, v in vals.items():
            if v in text:
                bad.append(f"{rel}: contains the value of {name}")
        if str(rel).startswith("dist/"):
            text = ALLOW_URL.sub("", text)
        for rx, what in PATTERNS:
            for m in re.finditer(rx, text):
                ctx = text[max(0, m.start() - 80):m.start()]
                if str(rel).startswith("dist/") and ("url=" in ctx or "click" in ctx):
                    continue
                bad.append(f"{rel}: looks like a {what}")
                break
    if bad:
        print("SECRET CHECK FAILED:\n  " + "\n  ".join(sorted(set(bad))[:50]))
        sys.exit(1)
    print(f"secret check ok ({len(vals)} secret values, {'repo' if repo_only else 'repo + dist'})")


if __name__ == "__main__":
    main()
