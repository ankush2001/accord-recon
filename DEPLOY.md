# Deploying

accord-recon is half of a pair. The full walkthrough for both services —
Neon for Postgres, Upstash for Redis, Render for the services — lives in
[obol-ledger/DEPLOY.md](https://github.com/ankush2001/obol-ledger/blob/main/DEPLOY.md).

The short version for this service alone:

1. Create a Postgres database on [Neon](https://neon.tech). The free tier is
   permanent; Render's free Postgres expires after 30 days and is then deleted
   with its data.
2. On [Render](https://render.com): New → Blueprint → connect this repo. The
   `render.yaml` is picked up automatically.
3. Set the one variable it asks for:

   ```
   ACCORD_DATABASE_URL = <Neon connection string, pasted unedited>
   ```

   Both `postgresql://` and `postgres://` are accepted — the driver name is
   added at startup, so the string can come straight from Neon's dashboard.

4. Render generates `ACCORD_WEBHOOK_SECRET`. Copy it into the ledger's
   `OUTBOX_SIGNATURE`, or the ingest endpoint stays open to anyone who can
   reach it — and a reconciliation against injected entries reports all clear.

Alembic migrates on startup. `/health` returns `degraded` rather than `ok` if
the database is unreachable, so a misconfigured URL is visible immediately
rather than at the first real request.

**Free instances sleep after 15 minutes idle.** The first request wakes this
one in a few seconds — Python starts far faster than the JVM next door.
