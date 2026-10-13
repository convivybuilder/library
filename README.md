# library

Builder's website is [convivybuilder.org](https://convivybuilder.org). It carries the documentation
and the install pages.

The **Library** keeps everything a project knows and serves its work queue. Inside Convivy it is
the Keel's retrieval and arbitration service: a process with its own storage
that clients call over a versioned protocol. It holds three vaults.

- **The Chart House** — work items, epics, Courses, and the leases that arbitrate them. This is the
  lock state the fleet's admission control depends on.
- **The Archives** — signals, and the run record.
- **The Reference Room** — the reviewed documents, whose content lives as files in the `knowledge`
  repository because review means a diff.

Until Phase 3 the Library was a set of modules inside `convivy-orient`, which made Orient the
client, the store and the API at once. Extracting it makes Orient a client like any other
(ADR-0022).

## What this repository holds, and what it does not

**This repository holds code.** Two of the three vaults keep their data outside it: the Chart House
is a database on the Mac Studio outside any repository, and the Archives are a database plus blob
storage. Cloning `library` gives you neither, **so both need backups that are not git.**

**`agentctl` does not live here.** ADR-0021 makes the Library and `agentctl` separately deployable
executables talking over a wire — many devboxes each running their own `agentctl` against one
Library — and that split is the most important property in the architecture. The two halves stay
two repositories.

## Installing on a fresh machine

Five steps, in order, and nothing runs any of them for you:

```
install -d -m 700 ~/.library
LIBRARY_DB=~/.library/library.db python -m alembic upgrade head
LIBRARY_DB=~/.library/library.db .venv/bin/python init_vectors.py
.venv/bin/python bootstrap_library.py "Ada Lovelace"
.venv/bin/python mint_credential.py --file agents --name orient --out ~/.orient-control/library-token
.venv/bin/python mint_credential.py --file clients --name orient --out ~/.orient-control/library-client-token
```

`install -d -m 700 ~/.library` creates the directory `LIBRARY_DB` names, at 0700 — SQLite needs the
directory to exist before `alembic upgrade head` can create the database file inside it, and the
directory must stay 0700 since it holds credential material. `alembic upgrade head` builds the
schema. `init_vectors.py` (library#155, not yet merged — this step needs it) adds the two vec0
tables autogenerate cannot see. `bootstrap_library.py` makes the first administrator and mints the
first approved-client credential, once; both it and the two steps before it need `LIBRARY_DB` set.

`mint_credential.py` mints every credential after the first, one call per credential, and needs no
database — it writes straight to `identity.TOKENS` (`--file agents`,
`~/.library/api-tokens.json`) or `identity.CLIENT_TOKENS` (`--file clients`,
`~/.library/client-tokens.json`), the same two files `identity.resolve` reads, so a credential it
mints authenticates with no restart. `--file agents` resolves as `agent` unless `--kind system` is
given; `--file clients` always resolves as `approved_client`, and there alone `--holds-reserved` and
`--withholds` narrow which human-reserved capabilities the credential carries. `--out PATH` writes
the token straight to PATH at mode 0600 from creation, rather than printing it — redirecting stdout
with `>` instead creates the file at the shell's umask (usually 0644, world-readable) until a later
chmod runs.

Orient needs one credential from each file, the worked example above: `library-token` (`agent`) for
`agentctl` and Orient's own tooling, and `library-client-token` (`approved_client`) for the
human-reserved calls Orient's control surface makes on a verified administrator's behalf.

## The protocol

The contract document lives in the KB and covers retrieval, checkout, status write-back, and the
token endpoints Phase 5 fills in. **It carries a version from the first commit**, because a
protocol that gains versioning later has already shipped one unversioned generation it must
support forever (ADR-0021).

Retrieval is deterministic. No model runs on the checkout path (ADR-0019).

## Status

Phase 3 of the Keel's build plan, in build now. The plan is
`~/Convivy/substrate/keel-build-plan.md`; the phase's epic and its children are in
the Chart House under the `keel` project.

The Library's service identity will become the only writer to each vault's backing store
(ADR-0020). The named git bypass vectors get their controls before that is announced, since a
write path that can be bypassed is not one.

## The shadow, measured and retired

The shadow ended when the Library's reads matched Orient's for a full period against live
traffic, which was a measurement rather than a judgement. The comparison harness took it: it
asked both services the same question under every credential in the token file and reported
where the answers differed. It exited non-zero when the two disagreed about rows, so it could
gate the cutover instead of being read by eye. A contract difference alone — an envelope field
only one side answers, a column only one side returns — was reported and did not fail the run.
Findings never got normalised away; the first run found this service returning raw SQLite
timestamps where Orient returns RFC-3339, and a harness that coerced both sides before comparing
would have called that agreement.

The harness is retired now that the shadow is over.

## The individuation bundle

The Library's side of `platform/decisions/0085`: the Bosun asks for a token
naming the documents one agent needs for one task, and the individuator redeems
it once for those documents, so nothing on the redeeming side reads the shared
knowledge checkout. The design record, including why the token has its own table
and not `call_slips`, is `bundles.py`'s module docstring. Both routes are
additive within `Library-Protocol: 1`, so a client that predates them sees a 404
for each.

**`POST /bundles`** (capability `bundle.issue`) takes `{"agent": "<roster name>"}`
plus exactly one of `"item_id"` or `"repo"`, and optionally `"variant"` (default
`daytime`). The profile selected is `<project>/<variant>`, where `project` is the
item's product, or the named repository. It answers 201:

```json
{"token_id": "bundle_01...", "agent": "reviewer",
 "scope": {"kind": "item", "item_id": "issue_01...", "project": "library", "variant": "daytime"},
 "documents": [{"id": "agent-platform/agents/reviewer", "kind": "agent", "status": "current",
                "body_sha256": "3be8...", "source_ref": "...", "...": "no body"}],
 "count": 17, "expires_at": "2026-09-18 23:31:47.849393"}
```

**`POST /bundles/redeem`** (capability `bundle.redeem`) takes `{"token_id": "..."}`
and answers 200 with the same shape, each document now carrying `body`. Redeem
once, before `expires_at` (300 seconds after issue). A copy of a document on disk
is stale when `sha256(copy.encode("utf-8")).hexdigest()` differs from that
document's `body_sha256`.

**Every refusal names what it found, and none degrades to a smaller bundle:**

| Status | When |
|---|---|
| 400 | malformed request; both or neither of `item_id` and `repo` |
| 404 | no such item, or none this identity may see (one wording); no such bundle token |
| 422 | the agent is not on the roster; no profile `<project>/<variant>` |
| 409 at issue | a document the profile selects is not `current` (`documents[]` lists each with its `status`); an audience filter matches nothing (`kind`, `audience`, `matched: 0`, `candidates`, `audiences_present`); the profile selects no definition for the agent |
| 409 at redeem | the token was already redeemed; it expired; a document it names is `missing`, `not-current` or `changed` (`documents[]` lists each with `reason`). The last does not consume the token |

An audience filter that matches nothing is a refusal because an empty bundle ships
an under-equipped agent and looks identical to a served one. As of 2026-09-18 that
is the answer for a `platform-coder`: no skill names it in `audience`, they name
`coder`. No role name is mapped to another here.

## Where things are

| What | Where |
|---|---|
| Decisions | `knowledge/projects/platform/decisions/` — ADR-0019 through ADR-0023, ADR-0026, ADR-0030 |
| The build plan | `~/Convivy/substrate/keel-build-plan.md` |
| The client that will call it first | `convivy-orient` |
| The other half of the split | `agent-platform` (`agentctl`) |

This repository belongs to the **keel** project, whose cockpit is
`~/Convivy/substrate`.
