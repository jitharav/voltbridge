# CI/CD setup (one-time)

Two GitHub Actions workflows are included in `.github/workflows/`:

| Workflow | File | What it does | When |
|---|---|---|---|
| **CI** | `ci.yml` | Runs the 25-check protocol test suite on **Ubuntu + Windows × Python 3.9 / 3.11 / 3.12**, and builds the dashboard | every push + PR |
| **Deploy** | `deploy.yml` | Builds the dashboard and publishes it to **GitHub Pages** (a live URL) | every push to `main` |

## Steps to activate

1. **Commit and push** the new files:
   ```
   git add .github vite.config.js README.md CI_CD_SETUP.md
   git commit -m "Add CI (test matrix + build) and Pages deploy"
   git push
   ```
   The **CI** workflow runs immediately — check the **Actions** tab for green checks.

2. **Enable Pages** (for the live dashboard):
   Repo **Settings → Pages → Build and deployment → Source → "GitHub Actions"**.
   The next push runs **Deploy**; the live URL appears in the workflow summary and at
   `https://<your-user>.github.io/<your-repo>/`.
   - Public repo: free.
   - Private repo: GitHub Pages needs a paid plan (GitHub Pro). If private and unpaid,
     just delete `deploy.yml` and keep `ci.yml` — the tests/badge still work.

3. **Fix the badge URLs**: in `README.md`, replace `OWNER/REPO` and `OWNER`/`REPO`
   with your actual GitHub username and repository name. The badges then show
   live pass/fail status.

## Notes

- The deployed dashboard runs the **standalone simulation** (the live bench streams
  over `localhost`, which isn't reachable from a public page). That's the intended
  safe demo — anyone with the link sees it run.
- CI uses `npm ci`, so keep `package-lock.json` committed (it already is).
- To also run the suite on every push without Pages, keep only `ci.yml`.
