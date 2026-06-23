# DC Fleet Daily Status Report

Pulls Fleetio vehicle + assignment data twice a day and updates a Confluence page with
status counts, per-vehicle tables, and a change log.

## Setup

1. Create a new public (or private) repo on GitHub and push these files.

2. Generate an Atlassian API token at https://id.atlassian.com/manage-profile/security/api-tokens

3. In your repo, go to **Settings → Secrets and variables → Actions** and add:

   | Secret | Value |
   | --- | --- |
   | `FLEETIO_API_TOKEN` | Fleetio API key |
   | `FLEETIO_ACCOUNT_TOKEN` | Fleetio account token |
   | `ATLASSIAN_EMAIL` | your Atlassian login email |
   | `ATLASSIAN_API_TOKEN` | Atlassian API token from step 2 |
   | `ATLASSIAN_DOMAIN` | e.g. `appliedintuition.atlassian.net` |
   | `CONFLUENCE_PAGE_ID` | numeric ID of the page to update |

4. The workflow runs at **15:00 and 22:00 UTC** — equivalent to 8am and 3pm PDT.
   When DST ends in November, these become 7am and 2pm PT — adjust the cron lines in
   `.github/workflows/dc-fleet-daily.yml` if you want strict local times year-round.

5. To trigger manually: **Actions tab → DC Fleet Daily → Run workflow**.

## How it works

- `dc_vehicle_ids.json` — the 64 Fleetio IDs that comprise the DC fleet
- `state.json` — auto-committed by the workflow; stores only `{status, notes}` per vehicle
  (no operator names) so the next run can diff
- The script skips republish if only operators changed (status + notes identical to last run).
  Set `SKIP_IF_ONLY_OPERATORS=0` in the workflow env to disable.

## Local testing

```bash
pip install -r requirements.txt
export FLEETIO_API_TOKEN=...
export FLEETIO_ACCOUNT_TOKEN=...
export ATLASSIAN_EMAIL=...
export ATLASSIAN_API_TOKEN=...
export ATLASSIAN_DOMAIN=appliedintuition.atlassian.net
export CONFLUENCE_PAGE_ID=...
export DRY_RUN=1                  # print body instead of publishing
python dc_fleet_daily.py
```

Remove `DRY_RUN=1` to publish.

## Notes

- `state.json` is committed back to the repo on every run that changes it. If your repo
  is public, only `{status, notes}` per vehicle is exposed — no operator names.
- The script writes only to Confluence. It does not post to Slack or Jira.
- If Fleetio rate-limits the assignments endpoint mid-pagination, the script falls back to
  the `driver` field on `/vehicles` and notes this in the change log.
