# Cloud Deployment of the Churn Data Warehouse and Model

**Owners & Developers:** MuhammaD_Wallah and the GROUP are the owners and Developers of this model.

Live web app: https://muhammad-telco-churn-analytics.streamlit.app/ ·
Source code: https://github.com/MuhamadWallah/telco-churn-analytics ·
Showcase: https://muhamadwallah.github.io/telco-churn-analytics/

## Deployment summary

To make the warehouse and the model usable outside the development PC, the project was deployed on the
free plans of three services. Neon (serverless PostgreSQL 18) hosts the data warehouse,
Streamlit Community Cloud hosts the public web app, and GitHub Actions runs the maintenance jobs. The
cloud warehouse is built by the same SQL scripts as the local one, so both hold the same 7,043 customers,
and all 123 validation checks passed.

The web app reads the OLAP layer (materialized views and the churn cube) and the stored churn scores. The
final model, a Random Forest chosen for the highest cross-validated recall among models with precision of
at least 0.50, reached a test recall of 0.773, precision 0.527, F1 0.627 and ROC-AUC 0.843. The model file
is versioned in the repository with its metrics, so the app and the scoring job always use the same model.

Security follows least privilege. The public app connects as `churn_app_reader`, a role that can only
read the OLAP schema and two model tables, and every app transaction is also opened read-only. Owner
credentials exist only in a git-ignored local file and an encrypted GitHub secret.

On the first day of each month, GitHub Actions validates the warehouse, refreshes the materialized views,
re-scores every customer and records data drift (Population Stability Index). Each quarter it retrains the
model and replaces it only if recall and ROC-AUC do not fall. Because Neon's free plan keeps only six
hours of history, monthly `pg_dump` backups are stored off-line; a test restore reproduced all 27 tables
and materialized views exactly.

Two limitations remain. The database runs in Frankfurt while Streamlit runs apps in the United States,
which adds latency that a one-hour query cache hides for most page views. The sample is also static, so
drift stays at zero until new customers are loaded, and all results show associations, not causes.

## Architecture

```
                  +---------------------------------------+
                  | GitHub repository (public)            |
                  | MuhamadWallah/telco-churn-analytics   |
                  | sql/  model/  app/  workflows         |
                  +---------------------------------------+
                          | redeploy on push        ^ commits run summary
                          v                         | and promoted models
+--------------------------------+      +------------------------------------+
| Streamlit Community Cloud (US) |      | GitHub Actions                     |
| public web app                 |      | monthly: validate, refresh,        |
| app/streamlit_app.py           |      |   re-score, log drift              |
| + model/churn_model.joblib     |      | quarterly: retrain, keep the new   |
| query cache: 1 hour            |      |   model only if it is not worse    |
+--------------------------------+      +------------------------------------+
   ^          | role churn_app_reader             | owner role (GitHub secret)
   |          | pooled connection, READ ONLY      | direct connection
visitors      v                                   v
(HTTPS)   +--------------------------------------------------------------------+
          | Neon serverless PostgreSQL 18 (AWS Frankfurt) - telco_churn_dw     |
          |                                                                    |
          | staging.telco_raw --ETL--> dw star schema --> olap views and       |
          | (copy of the CSV)          (1 fact, 6 dims)    materialized views  |
          |                            dw.churn_scores, dw.model_runs          |
          +--------------------------------------------------------------------+
                  ^                                     |
                  | build_warehouse.ps1 (sql/01-12)     | backup_neon.ps1
                  | model/train.py, model/score.py      | (pg_dump)
                  |                                     v
          +--------------------------------+  +------------------------------+
          | Developer PC (Windows)         |  | backups/*.dump (newest 6,    |
          | data/Telco-Customer-Churn.csv  |  | not in Git) + off-site copy  |
          +--------------------------------+  +------------------------------+
```

## Free-service limits

Checked on 23 October 2026 on each provider's own pages; free-plan limits change over time.

| Service | Free-plan limit | This project | When the limit is reached |
|---|---|---|---|
| Neon | 1 GB storage per project; 100 CU-hours of compute per month; 5 GB data transfer per month; 10 branches; 6-hour restore window; 1 manual snapshot; compute sleeps after 5 idle minutes | about 14 MB; an estimated 15-30 CU-hours per month; 1 branch; 1 snapshot | compute is suspended until the next month (compute or transfer), or writes fail (storage); no charge and no data loss |
| Streamlit Community Cloud | about 0.078-2 CPU cores and 690 MB-2.7 GB memory per app; sleeps after 12 hours without visitors | 1 public app | the app sleeps; a visitor wakes it in about a minute |
| GitHub Actions | free for public repositories on standard runners; scheduled workflows are disabled after 60 days without repository activity | 2 workflows | the schedule stops; the job's monthly commit counts as activity and prevents this |

Sources: https://neon.com/docs/introduction/plans ·
https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app ·
https://docs.github.com/en/billing/concepts/product-billing/github-actions ·
https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows
