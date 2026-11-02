# 5. Narrow redaction patterns behind a literal prefilter

- **Status:** Accepted
- **Date:** 2025-01-01

## Context

Every string returned to an MCP client, and every log line, passes through `redact()`. An earlier
implementation used a catch-all for any base64-ish run of 40+ characters. It false-positived
constantly - ARNs, S3 keys, AMI IDs and long tag values were all mangled, which quietly corrupted
legitimate results. Redaction also runs over every response from every account in a fan-out, so it
sits squarely on the hot path.

## Decision

Match only known secret **shapes** (`AKIA`/`ASIA` key ids, STS session tokens, JWTs, and
named key/value pairs such as `accessToken:`), and gate the whole thing behind a single combined
regex of the literal prefixes (`_PREFILTER`). Strings that trip the gate pay for the real patterns;
everything else returns after one cheap scan.

## Consequences

- Legitimate AWS data flows through untouched, so results stay usable.
- The common case - a string with no secret in it - costs one scan instead of four.
- The prefilter can never *cause* a miss: any string the real patterns match necessarily contains
  one of its literals.
- **Cost:** a secret format nobody anticipated is not redacted. Mitigated by defence in depth - no
  tool is supposed to place a credential in its output in the first place.
- **Cost:** the prefix list duplicates literals embedded in the patterns. Adding a pattern without
  its prefix would silently disable that pattern, so
  `tests/test_properties.py::test_the_prefilter_admits_every_string_the_real_patterns_match`
  generates strings from each real pattern and asserts the prefilter admits them, pinning the two
  lists together.

## Alternatives considered

- **Broad entropy or length heuristics.** Rejected: that was the previous implementation, and its
  false-positive rate made real output untrustworthy.
- **No prefilter.** Rejected on measurement: four regex passes over every string of every response
  in a wide fan-out is real, avoidable work.
