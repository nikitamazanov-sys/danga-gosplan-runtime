# Automatic repository history backup

Scope: Git objects and branch/tag history in this repository. This is not a full live-site backup. LFS payloads, submodule repositories, databases, uploads, private configuration and uncommitted hosting changes are excluded. Public Tilda/Sites exports in Git are only as fresh as their last explicit export.

The workflow runs daily at 03:17 Moscow time and after pushes to main. Each run saves separate refs under backups/<repository>/<timestamp-run-attempt>/ without force-push or deletion. Source refs preserve old deleted branches and tags. After a transfer, a fresh empty repository fetches only from that provider and runs git fsck; the receipt records independent restore.

GitHub transport uses its short-lived GITHUB_TOKEN, contents:write only for this repository. GitLab transport requires GITLAB_BACKUP_SSH_KEY in repository Actions secrets and a matching project-scoped read/write deploy key on private GitLab project 88950335, nikita-websites-backup/websites-backup. It grants no hosting, account administration or other GitLab project access. Proposed key validity: 90 days, to 2027-02-13. New access is not yet granted. Keep private keys outside Git and chat. GitLab host verification is pinned to the official ED25519 public key from https://docs.gitlab.com/user/gitlab_com/ .

Without the GitLab credential, the workflow still attempts the GitHub snapshot but deliberately reports failure for incomplete external backup. No success claim for four providers is possible from this workflow. A JSON receipt is kept as a small seven-day Actions artifact, while snapshots remain Git refs until the owner explicitly decides retention.

Local verification: independent GitHub/GitLab bare repositories; annotated tag preserved; source branch deletion does not remove old snapshot; colliding snapshot identifier rejects overwrite; restore from GitLab succeeds after the original source is made unavailable; backup refs are excluded from subsequent source enumerations.

All four repository jobs are preparation of code-history backup only. Existing production publication workflows are unchanged. Activation on main is committed with [skip ci] to avoid an unrelated application deployment. Branch backup/automatic-copy-20261115 is for the first actual run. Verify its receipt and GitHub/GitLab refs before claiming external backup enabled.
