# Oura Connector

A self-hosted connector for your [Oura ring](https://ouraring.com/) health data
on Cloud in a Bottle. It syncs the metrics your ring collects — sleep, activity,
readiness, and workouts — from the Oura Cloud API into a private SQLite database on your
own zone, shows them in a dashboard, and re-exposes them to your other apps
through the shared
[health-data service](https://github.com/imbue-openhost/health-data-service-spec)
(the same interface the Apple Health connector provides), so anything else you
run can read your health history without going back to Oura's cloud.

**Who it's for:** Oura ring owners who want to own and explore their data off of
Oura's servers, and Cloud in a Bottle users who want a single health-data source
their other apps can build on.

## Setup

Oura's API uses OAuth, so you connect it once with your own Oura developer
credentials:

1. Create an OAuth application in the
   [Oura Cloud developer console](https://cloud.ouraring.com/oauth/applications).
   - Set the **Redirect URI** to `https://<your-app>.<your-zone>/oauth/callback`.
   - Copy the **Client ID** and **Client Secret**.
2. Open this app and go to **Setup**. Paste the Client ID and Secret and click
   connect — you'll be sent to Oura to authorize, then redirected back.
3. Run an initial **Backfill** to pull your history (workouts included), then
   **Sync** keeps it up to date.

If you connected before workout support was added, your token lacks Oura's
`workout` permission: other data keeps syncing, the app shows a reconnect
notice, and reconnecting in **Setup** then running **Backfill** pulls your full
workout history.

Your Oura credentials and tokens are stored only in this app's private database
on your zone.

## What it does

- **Dashboard** (`/`) — recent sleep, activity, and readiness at a glance.
- **Sync / Backfill** — pull the latest data, or backfill your full history.
- **Provides the `health-data` service** (`/api/`) — other apps on your zone can
  read your metrics, time series, sleep sessions, and workouts through the
  Cloud in a Bottle service mesh (you grant access per app).
- **Workout dedupe** (opt-in, in **Setup**) — if another app also provides the
  `health-data` service (e.g. Apple Health), Oura workouts that overlap one of
  its workouts in time are hidden from the `health-data` API so consumers don't
  see the same session twice. They stay in this app's database, and turning the
  option off returns them. Checked on every sync (last 30 days) and across your
  full history after a backfill or when the option is switched on.

## Access

The app is owner-only: every page and API is gated by the Cloud in a Bottle
router, so only the zone owner (auto-recognized, no separate login) can reach the
dashboard, the OAuth setup, or the data. The `health-data` service is consumed by
other apps through the service mesh, not the public web.

## Links

- Oura — https://ouraring.com/
- Oura Cloud API — https://cloud.ouraring.com/v2/docs
- Health data service spec — https://github.com/imbue-openhost/health-data-service-spec
