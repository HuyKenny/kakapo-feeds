# kakapo-feeds

Nightly slim supplier feeds for [kakapo.cz](https://kakapo.cz) (holiday comparison).

- `scripts/atis-slim-feed.py`: ATIS XML (~890 MB) → cheapest future offers per hotel (~4 MB)
- `scripts/cedok-slim-feed.py`: Čedok SFTP XML (~3.8 GB) → slim feed in the same format

Both stream the source (`curl | iterparse`), so memory stays small, and upload the result to
the kakapo import Worker. Runs in GitHub Actions (`.github/workflows/feeds.yml`); all endpoints
and keys are repository secrets. No keys or data live in this repository
(`scripts/verify_no_secrets.py` runs before every job).
