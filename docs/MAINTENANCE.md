# Maintenance guide - Telco churn warehouse, model and web app

**Owners & Developers:** MuhammaD_Wallah and the GROUP are the owners and Developers of this model.

This page explains how to keep the cloud version healthy: what runs by itself,
what to check each month, what to do when something changes or breaks, and the
free-plan limits. Everything here is **free**; nothing needs a credit card.

| Part | Where it lives | Link |
|---|---|---|
| Web app (public, read-only) | Streamlit Community Cloud | https://muhammad-telco-churn-analytics.streamlit.app/ |
| Data warehouse + scores | Neon, project `telco-churn` (`purple-dust-93897460`), branch `production`, database `telco_churn_dw`, region AWS Frankfurt | https://console.neon.tech |
| Code, model, monthly job | GitHub `MuhamadWallah/telco-churn-analytics` (public) | https://github.com/MuhamadWallah/telco-churn-analytics |
| Showcase page | GitHub Pages | https://muhamadwallah.github.io/telco-churn-analytics/ |

---

## 1. What runs by itself

| When | What happens | Where to see it |
|---|---|---|
| **1st of every month**, 05:00 UTC (07:00 in Zimbabwe) | Workflow **Monthly refresh**: validation checks, refresh of the materialized views, re-score of every customer, one new row in `dw.model_runs` (with drift), then a small commit of `model/last_refresh.json` | GitHub > **Actions** > Monthly refresh |
| **1 Jan, 1 Apr, 1 Jul, 1 Oct**, 06:00 UTC | Same workflow, job **Retrain and keep the better model**: trains a new "candidate" model; it replaces the current model **only if recall and ROC-AUC do not get worse**. If replaced: commit of `model/`, Streamlit Cloud redeploys the app by itself, every customer is re-scored | GitHub > Actions > the run > job "Retrain ..." |
| Every push to `main` | Workflow **Build, Test & Deploy Model**: tests, Docker image, GitHub Pages showcase | GitHub > Actions |
| After 5 idle minutes | Neon switches the database computer off ("scale to zero"); the next query wakes it in a few seconds | Neon Console > Monitoring |
| After 12 hours without visitors | Streamlit puts the app to sleep; a visitor clicks **"Yes, get this app back up!"** and waits about a minute | the app link |

Words used here:
- **Workflow / job**: a list of steps that GitHub runs on its own computers.
- **Re-score**: the model gives every customer a new churn probability and a risk label (High / Medium / Low).
- **Commit**: a saved change in Git. The monthly commit also keeps the schedule alive (see section 8).

GitHub may start scheduled runs some minutes late when it is busy. That is normal.

---

## 2. The monthly 5-minute check (on the 2nd of each month)

1. **Did the job run?** GitHub > **Actions** > **Monthly refresh**: the newest run has a **green tick**.
   Red cross: open it, read the red step, and see section 7.
2. **Update your PC:** in the project folder, `git pull` (the job saved a commit; without the pull your next `git push` is refused).
3. **Is the app fine?** Open the app > **Model performance** > *Performance over time*: the newest row
   has today's date, and the drift status is **OK** (section 3 if not).
4. **Backup:** `powershell -ExecutionPolicy Bypass -File .\backup_neon.ps1`, then copy the newest file
   from `backups\` to a USB stick or Google Drive (section 5).
5. **Limits:** Neon Console > your project > **Usage** (or Monitoring): compute hours and storage are far below the limits in section 6.

After a **quarterly** run (January, April, July, October) also open the job **Retrain and keep the better model**
and read the step *Compare with the current model*: it says whether the new model was kept and why.

---

## 3. Data drift: what the status means and what to do

**Data drift** means today's customers look different from the customers the model learned from, so its
predictions may become less reliable. Every monthly run measures it with the **PSI** (Population Stability
Index), which compares how the churn probabilities are spread today with how they were spread at training time.

| Status | PSI | Meaning | What to do |
|---|---|---|---|
| **OK** | below 0.10 | no real change | nothing |
| **Watch** | 0.10 - 0.25 | some change | look at what changed in the data (new customers? a new price plan?); check again next month; you may retrain early (below) |
| **Act** | 0.25 or more | big change | **retrain now** (below), then check that recall on the *Model performance* page is still acceptable |

**Important:** the warehouse holds one fixed public sample (IBM Telco, 7,043 customers), and the model was
trained on that same data. So PSI stays **0.000 / OK** until **new customer data** is loaded. Drift becomes
meaningful only after section 4.

**Retrain early (without waiting for the quarter):** GitHub > Actions > **Monthly refresh** > **Run workflow** >
tick **"Also retrain the model"** > **Run workflow**. Afterwards `git pull` on your PC.

**Put an older model back ("roll back"),** for example if a new model behaves badly:
```powershell
python model/promote.py --list                                   # the archived versions
python model/promote.py --rollback 20261023-0259-random-forest   # the version you want back
git add model/ ; git commit -m "Roll back the model" ; git push  # Streamlit Cloud redeploys by itself
python model/score.py                                            # re-score with that model
```

---

## 4. Loading new customer data

1. **Backup first:** `.\backup_neon.ps1` (section 5).
2. Replace `data/Telco-Customer-Churn.csv` with the new file (same columns, same header).
3. Rebuild the cloud warehouse (re-runnable; the history in `dw.model_runs` and `dw.churn_scores` is kept):
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\build_warehouse.ps1 -Cloud -SkipMining
   python model/score.py          # scores + drift for the new customers
   ```
4. Look at the drift status (section 3). Retrain if it says **Act**.

---

## 5. Backups and restore

There are three safety nets. Use the first one that still reaches back far enough:

| Safety net | Reaches back | How |
|---|---|---|
| **Neon restore window** | **6 hours** (free plan) | Neon Console > your project > branch `production` > **Postgres database** > **Backup & Restore** > **Restore from history** > choose a time > **Next** > **Restore**. Neon keeps the state before the restore as a branch `production_old_...` (delete it later: max 10 branches). CLI: `neon branches restore production ^self@2026-10-23T04:00:00Z --preserve-under-name production_old` |
| **Neon snapshot** | the moment it was taken. The free plan allows **1** manual snapshot. Now: `baseline-2026-10-23` | before a risky change: `neon snapshots delete baseline-2026-10-23` then `neon snapshots create --branch production --name before-<change>`. List: `neon snapshots list` |
| **Backup files on your PC** | every month you ran it (the newest 6 are kept) | `backup_neon.ps1` / `restore_neon.ps1` below |

**Make a backup** (takes a few seconds, about 0.6 MB):
```powershell
powershell -ExecutionPolicy Bypass -File .\backup_neon.ps1
```
It saves `backups\telco_churn_dw_<date>_<time>.dump`, checks that the file can be read, and keeps the newest 6.
`backups\` is in `.gitignore`: a backup holds all the data, so it never goes to GitHub.

**Practise a restore** (your live database is not touched). Do this once a term, so you know it works:
```powershell
neon branches create --name restore-test --parent production
neon databases create --branch restore-test --name restore_check --owner-name neondb_owner
$test = neon connection-string restore-test --database-name restore_check
powershell -ExecutionPolicy Bypass -File .\restore_neon.ps1 -DatabaseUrl $test
neon branches delete restore-test          # free plan: max 10 branches, so always delete test branches
```

**Real emergency** (the live warehouse is damaged and the 6-hour window is too late):
```powershell
powershell -ExecutionPolicy Bypass -File .\restore_neon.ps1      # newest backup -> PRODUCTION
python model/score.py
```
It asks you to type `telco_churn_dw` before it changes anything. It restores in **one transaction**: if anything
fails, **nothing** is changed. It skips two entries that belong to Neon's own admin role (`cloud_admin`), because
Neon does not allow restoring them.

**Last resort - rebuild from the CSV** (loses only the history in `dw.model_runs`):
```powershell
powershell -ExecutionPolicy Bypass -File .\build_warehouse.ps1 -Cloud -SkipMining
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" "<owner connection string>" -f sql/11_app_reader_role.sql
python model/score.py
```
If `sql/11` gets a new reader password, update the app's secret too (section 8).

Tested on 2026-10-23: backup -> restore into an empty database **and** over a copy of the live database
(on a temporary branch): all 27 tables and materialized views had identical row counts, and the read-only
role kept its rights.

---

## 6. Free-plan limits (checked on 2026-10-23 - limits change, so re-check the linked pages)

**Neon, Free plan** - https://neon.com/docs/introduction/plans
| Limit | Free plan | This project uses |
|---|---|---|
| Storage | 1 GB per project | about 14 MB |
| Compute | 100 CU-hours per project per month | a few CU-hours (see below) |
| Compute size | autoscaling up to 2 CU | 0.25 - 2 CU |
| Data transfer out | 5 GB per month | small (results are cached for 1 hour) |
| Branches | 10 per project | 1 (`production`) |
| Restore window | 6 hours | - |
| Manual snapshots | 1 | 1 (`baseline-2026-10-23`) |
| Scale to zero | after 5 minutes idle (cannot be switched off) | - |

What happens at a limit: **no bill and no data is deleted.** If compute hours or data transfer run out, the
database is **switched off until the next month** (the app and the monthly job then fail until the 1st).
If storage is full, inserts and updates fail. To stay safe: keep the app's 1-hour cache, and never leave
a script polling the database in a loop.

*CU-hours*: 1 CU awake for 1 hour = 1 CU-hour. At 0.25 CU, 100 CU-hours = 400 hours awake. Because Neon sleeps
after 5 idle minutes and the app asks Neon at most about once an hour per page, a busy month uses roughly 15-30 CU-hours.

**Streamlit Community Cloud** - https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app
- About 0.078-2 CPU cores and 690 MB-2.7 GB memory per app (Streamlit's approximate figures).
- Sleeps after 12 hours without visitors; any visitor can wake it.
- A push to GitHub updates the app; a change to `requirements.txt` triggers a full redeploy (a few minutes).
- **Python version:** the app uses **3.14** (chosen in the app's Advanced settings). Streamlit Cloud supports
  only Python versions that still get security updates. Check this once a year.

**GitHub Actions** - https://docs.github.com/en/billing/concepts/product-billing/github-actions
- Free for public repositories on GitHub's standard runners.
- Scheduled workflows in a public repository are **switched off after 60 days without repository activity**
  (https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows).
  The monthly commit counts as activity. If it ever stops: Actions > Monthly refresh > **Enable workflow**.

---

## 7. When something goes wrong

| What you see | Likely cause | Fix |
|---|---|---|
| Job fails: *"Add the repository secret NEON_DATABASE_URL"* | the GitHub secret is missing or misnamed | Settings > Secrets and variables > **Actions** > **Secrets** tab > Repository secrets: `NEON_DATABASE_URL` = `DATABASE_URL_UNPOOLED` from `.env.local` (no quotes) |
| Job fails at *"Wake up Neon"* | owner password changed, or Neon switched off (monthly limit reached) | update the secret (section 8), or check Neon Console > Usage and wait for the 1st |
| Job fails at *"validate the warehouse"* | a validation check failed (bad data loaded) | read the FAIL rows in the log; fix the data; restore a backup if needed (section 5) |
| App: *"The database did not answer"* | Neon is waking up | wait 10 seconds, press **R** |
| App: *"password authentication failed"* | the reader password changed | update the app's secret (section 8) |
| App: yellow warning about the scikit-learn version | the app's Python/libraries differ from the model's | Streamlit app settings: Python 3.14; keep `requirements.txt` pinned; or retrain |
| `git push` refused (*"fetch first"* / *"non-fast-forward"*) | the monthly job made a commit | `git pull`, then `git push` |
| Monthly job stopped appearing | switched off after 60 days without activity | Actions > Monthly refresh > **Enable workflow** |
| App shows *"Yes, get this app back up!"* | the app slept (12 h without visitors) | click it and wait about a minute |

---

## 8. Passwords ("secrets") and how to change them

Never paste a password into a chat, an issue, a screenshot or a file inside the repository.

| Login | Used by | Stored in |
|---|---|---|
| `neondb_owner` (owner: full rights) | your PC (build, train, score, backup) and the monthly job | `.env.local` (git-ignored) and the GitHub secret `NEON_DATABASE_URL` |
| `churn_app_reader` (read-only) | the public web app | `.streamlit/secrets.toml` `[neon]` (git-ignored) and Streamlit Cloud > App settings > **Secrets** |

**Change the owner password** (for example after it was shown somewhere):
1. Neon Console > your project > branch `production` > **Postgres database** > **Roles** > the menu next to
   `neondb_owner` > **Reset password** > **Reset**. The old password stops working at once.
   Or: `neon api /projects/purple-dust-93897460/branches/br-divine-recipe-b9oyuv6w/roles/neondb_owner/reset_password -X POST`
2. `neon env pull` - writes the new connection strings to `.env.local`.
3. GitHub > Settings > Secrets and variables > Actions > `NEON_DATABASE_URL` > **Update** with the new `DATABASE_URL_UNPOOLED`.
4. Test: Actions > Monthly refresh > Run workflow (green tick).

**Change the reader password** (use the script, because it also re-checks all 11 read-only rules):
run `sql/11_app_reader_role.sql` again as the owner (it asks for the new
password), put the new password in the `[neon] url` line of `.streamlit/secrets.toml` **and** in Streamlit Cloud
> App settings > Secrets, then open the app.

**API keys:** `neon init` created an account-wide API key for coding tools (id 3457688). If you do not use those
tools: `neon api-keys list` and `neon api-keys revoke 3457688`.

---

## 9. Calendar

| How often | Task |
|---|---|
| Every month (2nd) | section 2 (5-minute check + backup) |
| Every quarter (after the 1st of Jan/Apr/Jul/Oct) | read the retrain decision; `git pull` |
| Every term | practise a restore (section 5) |
| Every year | re-check the free-plan limits (section 6) and the Python version on Streamlit Cloud; consider changing both passwords (section 8) |
