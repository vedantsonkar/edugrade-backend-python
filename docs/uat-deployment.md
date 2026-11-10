# UAT deployment

The Sydney UAT backend is deployed from the `master` branch of the public
`vedantsonkar/edugrade-backend-python` repository. GitHub Actions runs lint,
typing, tests and a container build on every push. A systemd timer on the EC2
instance checks GitHub every three minutes and deploys only when the latest
`master` commit has a successful `backend-ci.yml` push run. It downloads that
exact commit, builds locally, checks a candidate, and switches traffic. If the
replacement fails readiness, it restores the previous container. No AWS access
keys are stored in GitHub. A private repository would require a different
authentication design.

The candidate executes database migrations before it starts. Container rollback
does not reverse database migrations, so migrations must remain compatible with
the currently running version and must not discard data. Review migrations
before pushing them. The existing UAT database and S3 objects are not reseeded
or replaced during deployment.

## Runtime secrets

Non-secret settings and the two secret identifiers live in
`/opt/edugrade/backend.env`. The container loads values into its process at
startup using its EC2 instance role; credentials are not stored in that file.
The RDS-managed secret supplies the database username and password. The AWS
Secrets Manager secret `edugrade/uat/backend` contains `JWT_SECRET` and
`CRON_SECRET`; add optional `GEMINI_API_KEY`, `OPENAI_API_KEY`, `GLM_OCR_API_KEY`,
`GOOGLE_CLIENT_ID`, and `GOOGLE_CLIENT_SECRET` fields as needed.

To configure a provider key, open Secrets Manager in `ap-southeast-2`, select
`edugrade/uat/backend`, edit the JSON value, and add the relevant field. Preserve
the existing fields. Never commit secret values or paste them into CI logs. A
timer detects secret version changes and restarts the API; allow up to five
minutes. Rotating `JWT_SECRET` invalidates existing sign-in sessions. Enabling
OCR or Google OAuth also requires the corresponding non-secret application
configuration and provider-side setup.

The scheduled grading and audit tasks obtain their token from Secrets Manager.
The API serves `https://uat-api.edugrade.app`; the frontend's server-side
`BACKEND_API_URL` should be `https://uat-api.edugrade.app/api/v1`.

## Operations

The instance runs `edugrade-deploy.timer`, `edugrade-grading.timer`,
`edugrade-archive.timer`, and `edugrade-db-credentials.timer`. Use Systems
Manager Session Manager to inspect their services and journals. Do not print
container environment variables or secret values while troubleshooting.
The deployment service serializes with the credential-refresh service and keeps
the previous container for a code rollback. Monitor EC2 disk usage: locally
built images and build cache consume space and need periodic maintenance.
