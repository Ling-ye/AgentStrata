---
id: fresh-public-repository-bootstrap
type: public-contract
status: accepted
created: 2026-08-03
---

# Fresh Public Repository Bootstrap

## Summary

[KNOWN][HIGH] The public AgentStrata repository is bootstrapped from an audited
tracked-file tree, not from an existing Git object graph. Its first public
state is source-only, uses version `0.1.0.dev0`, and has no tag or Release.

[KNOWN][HIGH] The public product includes the declarative BotSpec runtime,
generic QQ and Feishu adapters and tools, the Codex backend, Console, MCP,
Evaluation, Wiki, search, memory, and development-task infrastructure. Private
product integrations and private operating values are outside this contract.

## Design

- Export the frozen source with `git archive` or an equivalent tracked-only
  operation into a directory without `.git`. Do not copy ignored files,
  caches, logs, credentials, local databases, reports, or private config.
- Finish source deletion, neutralization, dependency cleanup, documentation,
  and verification before creating the public Git repository.
- Keep generic Feishu document, sheet, Bitable, Wiki, messaging, adapter, and
  HTTP route-registry extension points. The HTTP server uses
  `CHATCOPILOT_HTTP_API_TOKEN`; route modules remain explicit configuration.
- Keep real runtime values in ignored `bots/<id>/local.env`. Public examples
  contain variable names and placeholders only.
- Run `scripts/check_public_repo.py` for public rules. Optional organization or
  project semantics come from `--private-literals-file`, which accepts only an
  external owner-owned `0600`, non-symbolic, single-link UTF-8 file with one
  exact literal per line. Findings never print the literal or repository path.
- After the final file-tree audit, the maintainer initializes `main` and makes
  one parentless commit. No old branches, tags, notes, replace refs, Releases,
  pull requests, issues, Actions runs, Wiki state, LFS objects, submodules, or
  other repository metadata are copied.
- The new remote starts private for CI and fresh-clone acceptance. Visibility,
  branch protection, tag protection, credential revocation, archival, signing,
  commit, and push operations remain maintainer-only actions.

## Acceptance

- The tracked tree, untracked candidates, built wheel, and sdist contain only
  public product code and neutral fixtures.
- The Lingye QQ BotSpec validates and remains runnable with its QQ, Codex,
  Wiki, MCP, memory, career-intelligence, and code-task capabilities.
- Generic Feishu adapter and tool-pack tests pass independently of any private
  product configuration.
- CLI help exposes `http-api-server` and no retired product-specific HTTP or
  upload commands. An empty route registry serves `/healthz` and returns 404
  for unknown routes.
- Current-tree scanning, optional private-literal scanning, SDD validation,
  architecture checks, tests, build, artifact inspection, and whitespace
  checks pass before Git initialization.
- The maintainer's final repository audit proves one commit, one `main` ref,
  no tags, no parent on the root commit, a clean `git fsck --full`, and an
  identical `main^{tree}` in a second fresh clone.

## Verification

The source-candidate phase uses:

```bash
python3 scripts/check_public_repo.py
python3 scripts/check_public_repo.py --private-literals-file /absolute/private/path
python3 scripts/check_sdd_specs.py
python3 scripts/check_architecture.py
python3 -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml
python3 -m pytest tests/unit tests/integration -q
python3 -m build
python3 scripts/verify_release_artifacts.py dist/*.whl dist/*.tar.gz
git diff --check
```

The maintainer performs the one-root Git and remote checks after exporting the
verified tracked tree. Those checks are intentionally not run in the private
source repository used to prepare this candidate.
