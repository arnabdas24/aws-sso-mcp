# 4. A token cache separate from the AWS CLI's

- **Status:** Accepted
- **Date:** 2025-01-01

## Context

The AWS CLI caches SSO tokens under `~/.aws/sso/cache/` keyed by a hash of the start URL. Reusing
it would spare users a second login. But that cache is an undocumented implementation detail with
a format AWS has changed before, and writing to it means writing a credential into a file another
tool owns and may rewrite concurrently.

## Decision

Keep an independent, documented per-org token cache at `<state_dir>/tokens/<org>.json`, created
mode `0600` and replaced atomically via a temp file and `os.replace`.

## Consequences

- The two tools can never clobber or corrupt each other's cached sign-in.
- The file format is ours, documented, and versioned on our terms.
- Permissions and atomicity are guaranteed by our own code rather than assumed.
- **Cost:** users sign in once for the AWS CLI and once more here. Judged acceptable against the
  risk of corrupting the CLI's credential store.
- **Cost:** we do not benefit from a CLI login the user has already performed.

## Alternatives considered

- **Read the CLI cache, never write it.** Tempting, and still possible later, but the format is
  undocumented and version-dependent; a silent parse failure would look like a login bug.
- **Share a single cache.** Rejected outright: concurrent writes to a credential file by two tools
  with different locking assumptions.
