"""Who is asking, and what they may do — checked HERE, in the service.

Contract §2. **A client-side check is not one.** `agentctl` and Orient are
separate deployables on separate machines, and a control the caller enforces is a
control the next caller omits. So every capability decision lives in this process,
and the client's job is to present a credential and read a refusal.

**Deny-by-default falls out of the shape**: an unknown role maps to an empty
capability set, so a token whose role nobody has thought about can do nothing
rather than everything.

Until Phase 5 this reads a plaintext token file — by default the Library's own
(`~/.library/api-tokens.json`), and on macstudio today the same file Orient does,
since `LIBRARY_API_TOKENS` points there — so the shadow can be verified with
the callers that already exist rather than a parallel set nobody uses. §7's
`POST /tokens/redeem` replaces it, and at that point the identity arrives in a
token the Library minted rather than a file both processes read.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import store


def resolve_tokens() -> Path:
    """Where `TOKENS` resolves: `LIBRARY_API_TOKENS` if set, else the
    Library's own file, beside `CLIENT_TOKENS`. A function rather than an
    inline expression so a test can call it again after changing `HOME` or
    the variable, without reloading the module."""
    return Path(os.environ.get(
        "LIBRARY_API_TOKENS",
        str(Path.home() / ".library" / "api-tokens.json")))


TOKENS = resolve_tokens()

# THE APPROVED-CLIENT CREDENTIAL, IN ITS OWN FILE, READ BY THE LIBRARY ONLY
# (ADR-0038). `api-tokens.json` above is the Library's own agent/system
# credential store — on macstudio today, via `LIBRARY_API_TOKENS`, the same live
# file Orient does — and adding a new `kind` value there would be a change to
# a running auth path this PR has no business touching. Same JSON shape as
# that file — RAW TOKEN → {name, products} — and every entry here resolves to
# `kind = "approved_client"`.
#
# **THE KEY IS THE BEARER TOKEN ITSELF, NOT A HASH OF IT.** `resolve()` below
# looks the incoming token up with `_client_tokens().get(token)`, and `_tokens()`
# is keyed the same way, so both of these files are plaintext secret stores and
# must be protected as such — 0600, in a 0700 directory. This comment said "token
# hash" until 2026-09-18, which is the dangerous direction to be wrong in: a
# reader who believes the file holds only digests has no reason to guard it, and
# `credential_bindings` storing a real SHA-256 makes the claim plausible. Every
# existing test fixture and `bootstrap_library.py` key it by the raw token.
CLIENT_TOKENS = Path(os.environ.get(
    "LIBRARY_CLIENT_TOKENS", str(Path.home() / ".library" / "client-tokens.json")))

# Capability sets per role. Deny-by-default: a capability nobody thought to
# grant a role is absent rather than assumed, so a gap in this table refuses
# rather than admits.
ROLE_CAPS: dict[str, frozenset[str]] = {
    "agent": frozenset({"item.read", "course.read", "signal.read",
                        "reference.read", "reference.checkout",
                        "reference.admit",
                        "item.create", "item.claim", "item.resolve",
                        "item.comment", "course.write", "signal.create",
                        "policy.read", "ledger.read", "ledger.spend",
                        "merge.admit", "merge.report", "token.redeem",
                        "roster.read", "review_record.read",
                        "bundle.issue", "bundle.redeem", "signing.verify"}),
    "system": frozenset({"item.read", "course.read", "signal.read",
                         "signal.customer", "reference.read",
                         "reference.checkout", "reference.admit",
                         "item.create", "item.claim", "item.resolve",
                         "item.comment", "course.write", "signal.create",
                         "signal.create_customer",
                         "signal.create_review",
                         "review_record.create", "review_record.read",
                         "policy.read", "ledger.read", "ledger.spend",
                         "merge.admit", "merge.report", "token.redeem",
                         "roster.read", "bundle.issue", "bundle.redeem",
                         "signing.verify"}),
}

# **`signing.verify` GATES `GET /signing-certificates/{fingerprint}`**
# (platform/contracts/library-protocol §2.2), the read `kb apply` makes to
# check a signed knowledge save's certificate before writing. **Its own
# capability, not a reuse of `reference.admit`** — the route answers a
# question about a signing certificate, a resource `reference.admit` (which
# gates a knowledge write's admission) has nothing to do with, and the
# deny-by-default table refuses a capability that was never named for a
# route rather than one that happens to share a caller with another.
#
# **On BOTH literal role sets, `agent` and `system`**, matching `token.
# redeem`'s reason above: the two are parallel literals rather than one
# derived from the other, so a capability meant for both has to be written
# into both or the role that missed out simply refuses with nothing in the
# table looking wrong. `kb apply` runs as whichever of the two its own
# credential resolves to.
#
# **It is not human-reserved.** §2.2 states the grant directly ("granted to
# the agent and system roles ... not human-reserved and not product-
# scoped"): checking whether a signing certificate is active is a question,
# not an act only a verified human's client may take, so it is absent from
# `HUMAN_RESERVED_CAPS` and reaches `approved_client` structurally, through
# the `system` union below, rather than as a listed exception to it.

# **`machine.verify` GATES `GET /machine-certificates/{fingerprint_sha256}`**
# (platform/decisions/0066), the read the Bosun makes against the Library's
# `machine_certificates` registry when a session registers, in place of the
# Bosun keeping its own list — one enrollment and one revocation then govern
# both servers.
#
# **ON NO ROLE SET AT ALL, UNLIKE `signing.verify` ABOVE — not `agent`,
# not `system`, not `approved_client`.** `signing.verify` serves `kb apply`,
# which authenticates as whichever of `agent` or `system` its credential
# resolves to, so that capability belongs to both roles outright. This one
# has exactly one caller, the Bosun's own registration check, and nothing
# argues for handing every `agent` and `system` credential on the machine —
# the github-capture poller and every dispatched coder included — a read of
# the registry that decides which machines may authenticate a session.
#
# **GRANTED ONLY THROUGH AN ENTRY'S OWN `grants`** (`mint_credential.py
# --grants machine.verify`, `--file agents`, is the supported path; a
# hand-edited `client-tokens.json` entry's `grants` reaches it too, because
# `_entry_capabilities` reads `grants` on every kind), so it is in
# `GRANT_ONLY_CAPS` below rather than a role literal: naming it in
# `ROLE_CAPS` would hand it to
# every credential of that role, and the whole point is that only the one
# credential minted for the Bosun holds it. It is not human-reserved either
# — `--file agents` cannot name a human-reserved capability in `grants` at
# all (`_entry_capabilities`), and this one is a question about a machine's
# certificate, not an act reserved to a verified human's client.
#
# **`ledger.spend.subscription` GATES `lane: "subscription"` ON `POST
# /ledger-entries`** (issue_01M35QG5NA13JE8FQVM9XET4HD, platform/decisions/0057).
# `ledger.spend` alone, held by every `agent` and `system` credential, says a
# caller may report spend; it says nothing about which LANE it may claim, and
# `subscription` is the one lane `ledger_days` and every threshold and
# checkout check skip. Without a second gate, any `ledger.spend` holder could
# tag real, metered-gateway spend `"subscription"` and keep it out of
# enforcement while it still reads as reported.
#
# **ON NO ROLE SET AT ALL, for `machine.verify`'s reason above**: handing this
# to every `agent` and `system` credential is the exact gap the capability
# exists to close, so `ROLE_CAPS` never names it, on any role including
# `approved_client`.
#
# **GRANTED ONLY THROUGH AN ENTRY'S OWN `grants`** (`mint_credential.py
# --grants ledger.spend.subscription`, `--file agents`), the same path
# `machine.verify` takes: a deliberate, auditable grant to the one credential
# that can back the claim — the Bosun's subscription reporter — rather than a
# role everything of that kind inherits. It is not human-reserved: `--file
# agents` cannot name a human-reserved capability in `grants` at all
# (`_entry_capabilities`), and the Bosun's reporter credential is not a
# verified human's client.
GRANT_ONLY_CAPS = frozenset({"machine.verify", "ledger.spend.subscription"})

# **`roster.read` GATES `GET /roster`, `GET /profiles` AND `GET /profiles/
# {project}/{variant}`** (issue_01M19QPV0J72EK1NDB0H8KT5EM), the read
# surface over the two tables `roster.write` already gates for writes below.
#
# **A NEW CAPABILITY RATHER THAN REUSING `reference.read`**, because the
# rows these routes serve live in the control database `crew_registry.py`
# writes to, not the knowledge index `reference.py` reads — `policy.read` is
# the existing neighbour this follows: a control-database read served to
# every agent and system credential, its own literal entry rather than a
# reuse of a capability that happens to be granted to the same callers today
# for an unrelated reason.
#
# **On BOTH literal role sets, `agent` and `system`, matching `policy.read`
# beside it** — the two are parallel literals rather than one derived from
# the other (`token.redeem`'s comment above states why a shared grant has to
# be written into both), and there is no reason a crew's roster or its
# profiles should be visible to one and not the other: every current holder
# of `policy.read` is exactly who needs to resolve a profile before a
# dispatch. **Granting it does not widen `roster.write`** — that capability
# sits nowhere but `orchestrator`, unaffected by this addition, so an
# ordinary `agent` or `system` credential can read a profile's resolved
# document set without gaining any ability to change which agents exist or
# which documents a profile selects.

# **`bundle.issue` AND `bundle.redeem` GATE `POST /bundles` AND `POST
# /bundles/redeem`** (issue_01M2VCWQXTF5EGHD1X1J3P4RTJ), the individuation
# bundle's two halves (`bundles.py`, `platform/decisions/0085`). Two capabilities
# rather than one because the two acts belong to different parties: the Bosun
# asks the Library what a dispatch needs, and the individuator redeems the
# answer. Nothing distinguishes those parties by credential today, so both are
# granted to the same roles, and a later split of the two callers narrows one
# capability without touching what the other holder already relies on.
#
# **On BOTH literal role sets, `agent` AND `system`, for `token.redeem`'s
# reason above**: the two are parallel literals rather than one derived from
# the other, and the Bosun and the individuator authenticate with whichever of
# the two their credential resolves to. Written into one only, the other
# refuses with nothing in the table looking wrong. `customer_reader` and
# `orchestrator` derive from `agent` and `approved_client` from `system`, so all
# five kinds hold both.
#
# **NEITHER CAPABILITY WIDENS WHAT A CALLER MAY READ.** A bundle is Reference
# Room documents, which every `reference.read` holder already reads and which
# are deliberately not product-scoped (`reference.py`'s module docstring). What
# a bundle route adds is the selection, never access. The item path still scopes
# by the item's product, and answers an out-of-scope item exactly as an absent
# one. Nothing here is human-reserved, so neither capability appears in the
# `approved_client` union below.

# **`signal.create_review` GATES THE SIGNAL KINDS THAT RECORD A REVIEW OUTCOME**
# (issue_01M0R0BFE0CFHJM6XR5NG13PF4). A `review_verdict` or `review_comment`
# capture is the row a conditions-based merge admission would read — who approved
# a PR, at which head. If its writer can mint it, a coder wanting its own work
# merged writes its own approval, and the gate reads a field its subject controls.
#
# **`agent` is the identity the gate exists to exclude**, and the grant sits on
# `system` because the GitHub capture poller — the only current writer of these
# kinds, in `github_capture.py` — resolves to `kind: system` in api-tokens.json.
#
# **It follows the `signal.create_customer` shape rather than the approved-client
# literal one.** `approved_client` derives from `system` and `customer_reader`
# from `agent`, so a grant on the `system` literal reaches the former and stays
# off the latter structurally, and a later grant to `agent` is a deliberate edit
# rather than a copy-paste accident. The human-reserved capabilities are literals
# in the approved-client union for the opposite reason: they must NOT ride along
# with everything `system` holds, where this one must.

# **`review_record.create` GATES `POST /review-records` FOR THE SAME REASON
# AND ON THE SAME LITERAL, `system`** (issue_01M2HS3Q36RE5F82QXRN4Z0D0K). A
# review record is the row the merge gates the epic "All reviews are memory"
# builds will read, so the identity that can mint one must exclude `agent`
# for exactly the self-approval reason above — an `agent`-role coder wanting
# its own work merged must not be able to write its own `run-verified`
# record. The write route decides `auth` itself regardless of what the
# caller claims, which narrows the risk a self-write could do, but granting
# the capability to `agent` at all would still let a coder mint an
# `unverified` record naming any verdict it likes, which is a review that
# was never reviewed reaching the same read surface as one that was.

# **`review_record.read` IS GRANTED ON BOTH LITERAL ROLE SETS, `agent` AND
# `system`, MATCHING `signal.read`'S OWN PLACEMENT**
# (issue_01M2HS42EA7V4DEXQX1R1PWS9Y). The three readers the epic names span
# both: the Bosun's gates are an `agent`-kind caller (the comment on
# `merge.admit`/`merge.report` above states this directly), a session
# starting work is likewise `agent`-kind, and Deck and Orient authenticate
# as `system`-derived `approved_client`. Unlike `review_record.create`,
# there is no self-approval risk to keep off `agent` here — reading a
# record back, including an `unverified` one with its `auth_reason`, tells
# a caller nothing it could not already infer from having posted the review
# itself, and every route this capability gates still scopes by product
# before returning a row.

# **`token.redeem` IS GRANTED ON BOTH LITERAL ROLE SETS, `agent` AND
# `system`**, matching `merge.admit`/`merge.report`'s comment above for the
# same reason: `agent` and `system` are parallel literals rather than one
# derived from the other, so a capability meant for both has to be written
# into both or the role that missed out simply refuses with nothing in the
# table looking wrong. The Individuator redeems on the agent's behalf and
# authenticates with whichever of the two roles its own credential resolves
# to today (`identity.py`'s module docstring — Phase 5 still reads the same
# token file Orient does), so `POST /tokens/redeem` cannot be reached with
# only one of the two granted.

# **`merge.admit` AND `merge.report` ARE GRANTED ON BOTH LITERAL ROLE SETS,
# `agent` AND `system`** (`issue_01M0EQTMZMAB93V9FFP4AD52JK`). They reach
# `customer_reader` and `approved_client` from there, through the unions below
# that derive from those two.
#
# **`agent` and `system` are PARALLEL literals, not one derived from the other,
# and a capability meant for both has to be written into both.** Adding it to
# only one is a silent half-grant: the role that missed out simply refuses, with
# nothing in the table looking wrong. An earlier draft of this comment claimed
# `system` inherited from `agent` — it does not, and the grant above is in two
# places because it has to be.
#
# The Bosun is an `agent`-kind caller and neither route is human-reserved —
# asking whether a merge may happen is a question, the same way
# `/reference/admission` is "not human-reserved" — and the table is
# deny-by-default, so the capability has to be named explicitly rather than
# assumed from `reference.admit`, which gates a different resource.
#
# `merge.report`'s grant is sized for what `POST /merge/outcome` does TODAY —
# accept, validate, record a signal, answer — not for what it will do once
# reconciliation is real. Releasing a lease and moving a work item out of
# `pr-open` are privileged operations; when that lands, this grant may need
# narrowing to keep from handing every `agent` credential a lease-release it
# does not have today.

# **EVERYTHING `agent` HOLDS, PLUS PERMISSION TO READ CUSTOMER CONTENT — AND
# DELIBERATELY NOT PERMISSION TO CREATE IT.** Those two are the one write the
# role table exists to keep apart, and until now they were welded together:
# `agent` had neither, `system` had both, and capabilities are granted as whole
# roles, so a credential that needed to READ captured customer mail could only
# get it by also gaining the ability to MINT customer-class rows.
#
# **The gap was live rather than theoretical.** The Gmail capture job writes
# every captured message `privacy_class = customer`, so all captured mail is
# invisible to every `agent`-role credential, including the one the ops MCP
# tools present. A search does not refuse — the rows are simply absent, which is
# the correct disclosure behaviour on the read side and also why the absence
# reads as "not captured" rather than "not permitted", and cost a steward
# session most of an afternoon before anyone reached for the token file.
#
# The one-line alternative was `"kind": "system"` on that token. It would have
# worked and it would have handed `signal.create_customer` to every agent
# session using those tools (the operator, 2026-08-22, choosing the narrow version).
#
# **A union over `agent` rather than a third literal set**, for the reason the
# `approved_client` union below records: a copy is a thing that drifts. The
# absence of `signal.create_customer` here is therefore structural rather than
# remembered — it is absent because `agent` does not have it, so a later grant
# to `agent` would have to be a deliberate act rather than an accident of
# copy-paste. `tests/test_customer_reader_role.py` pins that absence anyway, because
# structural is not the same as asserted.
ROLE_CAPS["customer_reader"] = ROLE_CAPS["agent"] | frozenset({"signal.customer"})

# **EVERYTHING `agent` HOLDS, PLUS THE ABILITY TO CHANGE CREW COMPOSITION —
# A NEW KIND RATHER THAN A WIDER `system`** (the operator, 2026-09-04, on
# issue_01M19QP8S0QPKM0MN7XHYWNGKY: "the roster for a crew should be
# malleable and not need a pull request. the definition of each agent —
# whether in a crew or not — should be canon (in the KB)."). `roster.write`
# gates writing a `roster_agents` or `profiles` row through `crew_registry`
# — which agents exist, and which per-project profile selects which of
# them — and it must not land on `system`: the github-capture poller
# resolves to `kind: system` today, and granting it here would hand that
# poller the ability to change which agents exist. A kind nothing currently
# holds gives the capability somewhere to live without granting it to
# anyone yet; minting an actual credential of this kind for a co session is
# Bosun-side work, not this repo's.
#
# **The distinction this preserves: a crew's roster is malleable, but an
# agent's DEFINITION stays canon and pull-request-governed.** `crew_registry`
# never accepts a `model`, `tools`, `allowed_tools`, `disallowed_tools` or
# `gateway` field — those live in the Reference Room definition ARCH-134
# already protects — so this capability can never be used to plant a model
# pin outside review.
#
# A union over `agent` rather than a copy, matching `customer_reader` above:
# a copy is a thing that drifts, and every ordinary capability an
# `orchestrator` credential needs — reading courses, claiming items,
# spending against a ledger — is exactly what `agent` already grants.
ROLE_CAPS["orchestrator"] = ROLE_CAPS["agent"] | frozenset({"roster.write"})

# **WHICH `kind` VALUES A TOKEN FILE MAY NAME, AND `approved_client` IS NOT ONE
# OF THEM.** `api-tokens.json` is the Library's own agent/system credential
# store; a kind there that minted an approved client would let that file grant
# the human-reserved capabilities ADR-0038 puts behind a separate file the
# Library alone reads. Enumerated rather than derived from `ROLE_CAPS`,
# precisely so that adding a role does not silently make it nameable by a
# token — that decision stays a second, deliberate edit.
#
# `orchestrator` IS nameable, unlike `approved_client`: it is minted the same
# way `system` and `customer_reader` are — an entry in that token file —
# rather than through the separate approved-client file ADR-0038 reserves for
# a verified human's client. Nothing names it there yet; adding the entry is
# Bosun-side.
NAMEABLE_KINDS = frozenset({"system", "customer_reader", "orchestrator"})
assert "approved_client" not in NAMEABLE_KINDS

# EVERYTHING `system` HOLDS, PLUS THE HUMAN-RESERVED CAPABILITIES (ADR-0038).
# A union rather than a third literal set, so the two cannot drift apart the way
# five independent `== "customer"` comparisons once did (PR #4) — an approved
# client is never refused a capability `system` already has, because the union
# reads `system`'s set rather than a copy of it. `signal.ack` and `policy.write`
# appear nowhere else: neither `agent` nor `system` may hold either, at any
# protocol version, because the control is the credential boundary and not a
# check on what the caller claims to be doing.
#
# **THE HUMAN-RESERVED SET IS A NAMED CONSTANT, AND THE UNION READS IT.** A
# credential's own entry can now grant capabilities beyond its role's set (see
# `resolve_capabilities`), and the load-time refusal that keeps these
# unreachable from every role but `approved_client` has to name them. A second
# copy of the list, held by the check and not by the union, is the drift the
# comment above already refuses for `system`.
HUMAN_RESERVED_CAPS = frozenset(
    {"signal.ack", "policy.write", "ledger.grant", "ledger.void",
     "machine.write", "signing.write", "reference.promote", "person.read",
     "person.write", "epic.approve", "item.model"})
ROLE_CAPS["approved_client"] = ROLE_CAPS["system"] | HUMAN_RESERVED_CAPS

# `epic.approve` GATES `POST /items/{item_id}/approve`
# (issue_01M32CA70BNV0WEW150V3KYBJ9), the only route that sets an item's
# `maturity` to `approved`. **NAMED FOR THE ACT THE SPECIFICATION NAMES**
# ("approving an epic"), and the route refuses anything that is not an epic, so
# `item.approve` would promise a reach the route does not have.
#
# **DEFINED ON A ROLE AND THEN WITHHELD, because it cannot be entry-only.**
# `_entry_capabilities` refuses an entry naming a capability no role defines, so
# the capability has to exist in the union above. It sits in
# `HUMAN_RESERVED_CAPS` for the same reason the other eight do: `agent`,
# `system`, `orchestrator` and `customer_reader` never hold it, at any protocol
# version, and an entry naming it on one of those kinds is refused at load.
# An `approved_client` credential holds it by default, so a client that must not
# approve (the Bosun) is narrowed by a `withholds` entry in the client-token
# file. That step is the operator's and is not made here.
#
# **THE CAPABILITY IS THE AUTHORIZATION AND THE HUMAN IS THE RECORD.** The route
# also requires an `acting_human` (`attested_human`), which records who the
# client acted for and checks nothing about them, so the gate is this
# capability and never a human `kind` — `resolve()` produces no such kind.

# `item.model` GATES `POST /items/{item_id}/model`
# (issue_01M38WTBNAS3NBGEYR8349RR52), the only route that sets or clears the
# operator's model choice on a work item. **HUMAN-RESERVED FOR THE REASON THE
# GATE EXISTS**: the Bosun's autonomous path honours a model on an item only
# because the Library attests that an operator set it, so a field any agent
# could write would remove the roster's model pin across the whole overnight
# fleet. Like `epic.approve`, it is held by default by an `approved_client` and
# narrowed off one that must not set it by a `withholds` entry, and the
# capability is the authorization while `acting_human` (`attested_human`) is
# only the record.

# `person.read` GATES `GET /people/{person_id}/projects`
# (issue_01M2RSE08WRRB1RJY1ZCSMD6EG). Named explicitly here rather than
# granted on `agent`/`system`, because the epic that specifies this route
# names exactly three callers — Orient, Deck and the Bosun — and all three
# authenticate as `approved_client`. Nothing today needs an ordinary
# `agent` or `system` credential to resolve which projects a person may
# see, and a capability nobody asked for is scope this table does not need
# to carry; a later caller that turns out to need it is a deliberate grant
# rather than an inherited one.

# `person.write` GATES `POST /people/{person_id}/projects`
# (issue_01M2RSF4MMRT7PBHJJPQ2BKEC7), its own literal entry beside
# `person.read` and for the reason `signal.ack` and `policy.write` are: neither
# `agent` nor `system` may hold it, at any protocol version, because what it
# changes is what a person may see, and a role table that lets a shared grant
# ride along would hand that to every credential the grant reaches.
#
# **THE CAPABILITY IS HALF THE CONTROL, AND THE OTHER HALF IS A ROW.** Holding
# `person.write` proves the credential came from the approved-client file this
# process alone reads, which `NAMEABLE_KINDS` keeps `api-tokens.json` from
# minting. It does not say WHICH person is asking, so the route also requires
# `ident.actor` to be in `administrators`. Both, not either: the capability
# alone would let any approved client (Orient, Deck, the Bosun) change anyone's
# projects, and the row alone would let a token in that agent file, whose
# `name` this repo does not control, claim an administrator's id.
#
# **THE ATTESTED CHANNEL, FOR WHOEVER WRITES THE NEXT CLIENT.** No new kind of
# credential exists for this. `bootstrap_library.py` mints an approved-client
# entry whose `name` is a person id, and `resolve()` copies that `name` into
# `Identity.actor`. So `ident.kind == "approved_client"` and `ident.actor ==
# "person_..."` are both established by the Library from its own credential
# file, and nothing in the request supplies either. This is the shape ADR-0038
# prescribes: the token that carries a verified human's identity is the
# credential of the client that verified them, not a `human` token.
#
# **WHAT IT ESTABLISHES.** Which credential authenticated, and which person that
# credential names. **WHAT IT DOES NOT.** That a human was present, that they
# consented to this call, or that the client presenting the credential is acting
# for them at this moment: whoever holds the bearer token holds the
# administrator's authority, exactly as with any bearer credential. A client
# that mints or forwards such a token on a person's behalf is vouching for that
# person, and the Library cannot see whether it should. Issue one only to a
# process you would trust with the administrator's authority.
#
# **A credential whose `name` is a person id therefore carries that person's
# authority, so protect `client-tokens.json` as the secret store it is**
# (0600 in a 0700 directory, see `CLIENT_TOKENS` above). Anyone who can write
# that file can mint an entry named for an administrator; that is the same
# boundary `bootstrap_library.py` states for the file, accepted for the same
# reason.
#
# **`acting_human` IS NEVER READ ON THIS PATH.** `attested_human()` checks the
# credential kind, a non-empty string and `MAX_ACTOR`. It never checks that the
# person exists, is an administrator, or that the client may act for them. That
# is the right threat model for recording who a client says it acted for, and
# the wrong one for deciding what someone may do, so the write route decides
# from `ident.actor` alone and takes no `acting_human` from the request.

# `reference.promote` GATES `POST /reference/promotions`
# (issue_01M19QQZ76GSZYNH11JBDMA2EM), its own literal entry beside
# `machine.write` and for the same reason. Every other way an agent definition
# reaches the Reference Room is a pull request, and three mechanisms assume it:
# the KB write verbs refuse every `agents/` and `profiles/` path under any
# provenance, the merge is blocked in every session, and a dispatched session
# cannot push to `main`. Promotion is the one path that is NOT a pull request,
# so the capability admitting it must be unreachable from `agent` and `system`
# at any protocol version — otherwise building this route removes that
# protection while every document still describing it reads as though it holds.
#
# **THE ITEM SPECIFIED `if ident.kind != "human"` AND THAT WOULD HAVE BUILT A
# DEAD ROUTE.** `resolve()` produces `approved_client`, a `NAMEABLE_KINDS`
# value, or `agent`, and never `human` — ADR-0038 settled that no human calls
# the Library directly, so no phase mints a token bound to one. A route gated
# on a kind the service cannot produce refuses every caller forever while
# reading as a live control, which is the dead-capability shape a reviewer
# caught in PR #1 and the reason the comment above this union exists. The
# control ADR-0048 actually wants is the credential boundary, and
# `approved_client` is where it lives: the client has verified the human by
# whatever means it has, and the human travels as `acting_human` for the
# record rather than for the authorization.

# `machine.write` GATES `POST /machines/{machine}/certificates` AND
# `POST /machines/{machine}/revoke` (platform/decisions/0066), its own
# literal entry beside `policy.write` and for the same reason: registering
# or revoking a machine certificate is the human-attended act the whole
# design turns on ("the only thing that authorizes any issuance is the operator
# comparing a fingerprint and pressing a finger to a sensor"), so neither
# `agent` nor `system` may hold it, at any protocol version. The route also
# refuses unconditionally on `ident.kind != "approved_client"`, matching
# `write_policy`'s belt-and-suspenders shape, and records the acting human
# from `attested_human` rather than from `ident.actor`.

# `signing.write` GATES `POST /signing-certificates` AND
# `POST /signing-certificates/{fingerprint}/revoke` (platform/contracts/
# library-protocol §2.2), its own literal entry beside `machine.write`
# rather than a reuse of it. `machine.write` decides which fingerprint the
# mTLS listener trusts as a machine; `signing.write` decides whose knowledge
# saves a signature counts as authored by. A credential trusted for one act
# is not thereby trusted for the other, so an approved client can be
# narrowed by `holds_reserved` or `withholds` to hold either capability
# without the other. Neither `agent` nor `system` may hold it, at any
# protocol version, for the reason every capability in this set does not.

# `policy.write` GATES THE MCP-CONNECTOR/EXFILTRATION POLICY (ADR-0048), AND IT
# IS ADDED HERE RATHER THAN TO THE GENERAL ADMIN ROLE ON PURPOSE. `system`
# already carries more than `agent` — `signal.customer` and its friends — and a
# later grant of `system`'s capability set to some new role would silently
# widen who may grant tool access if `policy.write` rode along inside it. It is
# its own literal entry in this union instead, matching `signal.ack` beside it:
# neither `agent` nor `system` may hold it, at any protocol version, because the
# risk it guards against — an agent widening its own connector access — is
# exactly the risk ADR-0038's approved-client boundary already exists to keep
# off every capability an agent can reach.
#
# **This alone is not the whole control.** `identity.authorized` checks the
# capability; the write route ALSO refuses unconditionally on `ident.kind !=
# "approved_client"`, matching Orient's `push_api.py`/`blog_api.py` pattern —
# belt-and-suspenders, the same shape `_ack_request` already uses for
# `signal.ack`. A capability set correctly configured today is not a
# guarantee it stays that way; the route-level refusal holds even if this
# table is ever misconfigured.

# THE WRITE CAPABILITIES ARRIVED WITH THE FIRST WRITE ROUTE, WHICH IS THE THING
# THE PREVIOUS VERSION OF THIS COMMENT SAID IT WANTED TO AVOID. It claimed they
# were "declared and unused rather than absent", on the reasoning that a role
# table growing a capability at the moment the first write route lands is a table
# nobody reviewed — but the table held no write capability at all, so the comment
# described an intention rather than the code. Stating it plainly instead: these
# are new, they landed with the write surface, and they were reviewed then.
#
# They mirror Orient's grants exactly rather than being re-derived, because §4
# carries the semantics across as a data migration rather than a redesign. In
# Orient's matrix both `agent` and `system` hold `create`, `consume`, `comment`
# and `signal_write`, and only `system` holds `signal_customer` — which is why
# creating a customer-class signal is the one write the two roles differ on.
#
# TWO OF ORIENT'S TWENTY VAULT WRITES WERE DELIBERATELY NOT HERE, AND NOW THEY
# ARE. Acknowledging a signal and withdrawing that acknowledgment both required
# `kind == "human"` in Orient, and this service used to resolve every token to
# `agent` or `system` — there was no `human` to be, and porting them would have
# produced a route no caller could ever reach, the dead-capability shape a
# reviewer caught in PR #1. ADR-0038 is what changed: there is no future state in
# which a human calls the Library directly, so the token that carries a verified
# human's identity is not a human token — it is the credential of the client that
# verified them. `approved_client` is that credential, and `signal.ack` is the
# capability the ported routes gate on.

# THE HUMAN-RESERVED CAPABILITIES ARE ISSUED TO APPROVED CLIENTS ONLY, AND AN
# AGENT NEVER CARRIES ONE. No phase mints a token bound to a human, because no
# phase lets a human call the Library at all — the credential that reaches a
# human-reserved capability is `approved_client` (Orient or Deck, having already
# verified the human by whatever means it has), and the human travels as
# `acting_human` on the request body, attested by `attested_human()` below and
# recorded as the actor rather than the client. `agent` and `system` hold none
# of them, at any protocol version: ADR-0001's requirement is a capability, not
# a rule somebody remembers.
#
# The decision plane's own human-reserved capability, `decision.decide`, is
# gone along with the plane itself (issue_01M0ASMCB4F68PF3NJG7BV7K6C). The
# reasoning above carries forward to `signal.ack` and `policy.write`, the two
# that remain.


def _log(msg: str) -> None:
    print(f"library.identity: {msg}", file=sys.stderr)


@dataclass(frozen=True)
class Identity:
    """`actor` is the audit id. `kind` drives which human-reserved capabilities
    are reachable at all. `products` is the scope — `"*"` means every product,
    and an EMPTY set means none, which is a real state rather than a bug: a
    token registered with no scope can authenticate and read nothing.

    `grants` and `withholds` are the credential's OWN entry's capability
    deltas against its role, already validated by `resolve()`. Both are empty
    for every credential whose entry names neither, which is every credential
    that existed before they did."""
    actor: str
    kind: str
    products: frozenset[str] = field(default_factory=frozenset)
    grants: frozenset[str] = field(default_factory=frozenset)
    withholds: frozenset[str] = field(default_factory=frozenset)
    # None: the entry has no `holds_reserved`, and the credential holds every
    # human-reserved capability its role does. A set (even empty): it holds
    # exactly those of them and no other, whatever HUMAN_RESERVED_CAPS gains.
    holds_reserved: frozenset[str] | None = None

    @property
    def caps(self) -> frozenset[str]:
        return resolve_capabilities(self).caps


class CredentialStoreError(RuntimeError):
    """The Library's own client store could not be resolved. Fatal to serving:
    see `_client_tokens`."""


class _BadEntry(ValueError):
    """One credential entry whose capability fields cannot be resolved. What
    happens next depends on WHICH file it came from (client store: fatal;
    the agent/system file: that entry authenticates nobody)."""


@dataclass(frozen=True)
class ResolvedCaps:
    """A credential's capability set and where each member came from.
    `caps == from_role | from_entry`; `withheld` is what the role would have
    given and the entry took back, so it is in neither."""
    role: str
    caps: frozenset[str]
    from_role: frozenset[str]
    from_entry: frozenset[str]
    withheld: frozenset[str]
    # The human-reserved capabilities the role would have given and the entry's
    # `holds_reserved` did not list. Also in neither `caps` nor `withheld`.
    excluded_by_holds_reserved: frozenset[str]
    # A withholding naming something the role never had takes nothing today
    # and would the day a role change gives it.
    dead_withholds: frozenset[str]


def resolve_capabilities(ident: Identity) -> ResolvedCaps:
    """THE ONE PLACE A CREDENTIAL'S CAPABILITIES ARE COMPUTED. A route asks
    `authorized()`, which asks this; no route reads `ROLE_CAPS`.

    Role set, plus the entry's grants, minus the entry's withholdings. **A
    capability named in both grants and withholds is withheld**: deny wins when
    an entry contradicts itself. A capability in neither the role nor the entry
    is absent, as before, so an entry cannot make an unknown capability
    configurable (`_entry_capabilities` refuses unknown names at load).

    **The human-reserved subtraction is a second check, not the first.** Loading
    already refuses a non-`approved_client` entry that names one. Nothing
    outside `resolve()` constructs an `Identity` with grants today
    (`load_crew_registry` builds its orchestrator identities with none); this
    layer exists so a future caller that does cannot carry one past this line.
    """
    role_caps = ROLE_CAPS.get(ident.kind, frozenset())
    grants = ident.grants
    if ident.kind != "approved_client":
        grants = grants - HUMAN_RESERVED_CAPS
    excluded = frozenset()
    if ident.holds_reserved is not None:
        # Computed against the constant as it is NOW, so a capability added to
        # HUMAN_RESERVED_CAPS later is excluded without anyone editing the entry.
        excluded = HUMAN_RESERVED_CAPS - ident.holds_reserved
    caps = (role_caps | grants) - ident.withholds - excluded
    return ResolvedCaps(role=ident.kind, caps=caps,
                        from_role=caps & role_caps,
                        from_entry=caps - role_caps,
                        withheld=role_caps & ident.withholds,
                        excluded_by_holds_reserved=(
                            (role_caps | grants) & excluded) - ident.withholds,
                        dead_withholds=ident.withholds - role_caps)


def _known_capabilities() -> frozenset[str]:
    """Every capability some role defines, plus `GRANT_ONLY_CAPS` — a
    capability on no role at all, reachable only through an entry's own
    `grants`. Read at call time so it follows `ROLE_CAPS`, which tests
    replace."""
    return frozenset().union(*ROLE_CAPS.values()) | GRANT_ONLY_CAPS


def _entry_label(token: str, meta: dict) -> str:
    """How a refusal names an entry WITHOUT printing its bearer token: the
    `name` field plus a short hash of the token, enough to find the entry."""
    digest = hashlib.sha256(str(token).encode()).hexdigest()[:8]
    return f"{_safe_actor(meta.get('name', 'unknown'))!r} (token sha256:{digest})"


def _entry_capabilities(meta: dict, kind: str, label: str
                        ) -> tuple[frozenset[str], frozenset[str]]:
    """`(grants, withholds)` from an entry's `grants` and `withholds` lists, or
    `_BadEntry` naming the entry and the offending capability.

    Refused: a field that is not a list of strings; a `grants` entry naming a
    capability no role defines; a `withholds` entry naming a capability no role
    defines, for any `kind` but `approved_client` (see below); and — for any
    `kind` but `approved_client` — a human-reserved capability in EITHER list.
    The spec names `agent` and `system`; this covers `customer_reader` and
    `orchestrator` too, since every role but `approved_client` must keep those
    unreachable and nothing argues for a fourth exception.

    **A `withholds` entry naming a capability no role defines is kept, not
    refused — only for `kind == "approved_client"`, the client store.**
    Nobody can hold a capability no role defines, so withholding it changes
    nothing there — the subtraction in `resolve_capabilities` is a no-op for a
    name that was never in `role_caps` to begin with. Refusing the whole store
    over that is the trap `ROLE_CAPS` retiring a capability springs: a
    `withholds` entry written while every role still defined the name becomes
    unresolvable the moment the last role stops, and an unresolvable client
    store is fatal to serving — `_client_tokens()` raises, and
    `app.refuse_on_unresolvable_credentials` then answers 503 to every
    request, `/health` included. It is still reported, via `dead_withholds`
    in the credential report, which already covers a `withholds` entry naming
    a capability its OWN role lacks — the same field, one more way to reach
    it. `_entry_holds_reserved` below never needs to reconcile this against
    `HUMAN_RESERVED_CAPS`: a name no role defines is never a member of that
    set either.

    **Every other `kind` keeps refusing an unknown `withholds` name exactly as
    it did before this relaxation existed** (security-reviewer, PR #187):
    `api-tokens.json` already fails safely per entry — the bad entry
    authenticates nobody and every other token keeps resolving (`resolve`,
    below) — so there is no store-wide outage to trade that safety away for,
    and loosening it here would silently widen one credential from refused to
    its entire role the moment a withhold it carried went stale. `kind` is
    never attacker-chosen here: `resolve()` only ever calls this with
    `"approved_client"` for an entry already loaded from the client file, and
    `NAMEABLE_KINDS` excludes `"approved_client"` from what an api-tokens.json
    entry's own `kind` field can claim, so a non-client entry cannot name its
    way into the relaxation.

    `roster.write` in `grants` is refused for any `kind` but `orchestrator`,
    the same way: it is a role-only capability (see `ROLE_CAPS["orchestrator"]`
    above), but an entry can still grant a role's capability by name, and the
    github-capture poller resolves to `kind: system` in api-tokens.json today
    — an entry that named `roster.write` there would hand that poller the
    ability to change which agents exist, the exact reach the comment on
    `ROLE_CAPS["orchestrator"]` keeps off `system`. `withholds` is unaffected:
    narrowing a kind that never holds it is a no-op there.
    """
    known = _known_capabilities()
    out = []
    for field_name in ("grants", "withholds"):
        raw = meta.get(field_name, [])
        if not isinstance(raw, list) or not all(isinstance(c, str) for c in raw):
            raise _BadEntry(f"entry {label}: {field_name!r} must be a list of "
                            f"capability names")
        for cap in raw:
            if cap not in known:
                if field_name == "withholds" and kind == "approved_client":
                    continue
                raise _BadEntry(f"entry {label}: {field_name!r} names unknown "
                                f"capability {cap!r}; no role defines it")
            if kind != "approved_client" and cap in HUMAN_RESERVED_CAPS:
                raise _BadEntry(f"entry {label}: kind {kind!r} may not name "
                                f"human-reserved capability {cap!r}")
            if (field_name == "grants" and cap == "roster.write"
                    and kind != "orchestrator"):
                raise _BadEntry(f"entry {label}: kind {kind!r} may not name "
                                f"orchestrator-only capability {cap!r}")
        out.append(frozenset(raw))
    return out[0], out[1]


def _entry_holds_reserved(meta: dict, kind: str, label: str,
                          withholds: frozenset[str]) -> frozenset[str] | None:
    """The entry's `holds_reserved` as a set, None when the field is absent, or
    `_BadEntry`. Refused: any kind but `approved_client`, a value that is not a
    list of strings, a name outside HUMAN_RESERVED_CAPS, and a name also in
    `withholds` (the entry would contradict itself, and a refusal is safer than
    choosing which line wins). Every refusal makes the client store unresolvable,
    because dropping the field would widen the credential to every reserved cap.
    """
    if "holds_reserved" not in meta:
        return None
    if kind != "approved_client":
        raise _BadEntry(f"entry {label}: kind {kind!r} may not carry "
                        f"'holds_reserved'; only an approved_client does")
    raw = meta["holds_reserved"]
    if not isinstance(raw, list) or not all(isinstance(c, str) for c in raw):
        raise _BadEntry(f"entry {label}: 'holds_reserved' must be a list of "
                        f"capability names")
    for cap in raw:
        if cap not in HUMAN_RESERVED_CAPS:
            raise _BadEntry(f"entry {label}: 'holds_reserved' names {cap!r}, "
                            f"which is not a human-reserved capability")
        if cap in withholds:
            raise _BadEntry(f"entry {label}: {cap!r} is in both "
                            f"'holds_reserved' and 'withholds'")
    return frozenset(raw)


def describe_credentials() -> str:
    """What this process can authenticate, for a startup log.

    **A MISCONFIGURED DEPLOY IS OTHERWISE INVISIBLE UNTIL THE FIRST 401**
    (security-reviewer, PR #1). During the shadow the Library reads Orient's token
    file, so the two must share a Unix user or the file must be readable by both.
    Run them as different UIDs and this process fails closed with no startup error
    — correct behaviour, undiagnosable symptom. Saying the path and the count at
    boot turns that into one line in the log.

    **Both counts are reported, and the client count separately from the agent/
    system one.** A deploy with the client file missing or unreadable is otherwise
    invisible until the operator's first ack returns 403 — the agent/system count stays
    healthy throughout, so only a line naming the client file specifically catches
    it before then.

    **An unresolvable client store does not abort startup.** It is reported
    here and the service boots, so every request is answered 503 by
    `app.refuse_on_unresolvable_credentials` with a hint naming the fix. That is
    a better thing for an operator to read than a crash-loop traceback, and it
    fails closed either way.
    """
    n = len(_tokens())
    try:
        client_tokens = _client_tokens()
    except CredentialStoreError as exc:
        return (f"{n} credential(s) loaded from {TOKENS}; ERROR: the client "
                f"store {CLIENT_TOKENS} cannot be resolved, so every request "
                f"will be refused 503 until it is fixed — {exc}")
    c = len(client_tokens)
    if n == 0:
        agent_part = (f"WARNING: 0 credentials loaded from {TOKENS} — every "
                      f"agent/system request will be refused 401. "
                      f"LIBRARY_API_TOKENS overrides this path; check the file "
                      f"exists and is readable by this user, or point it "
                      f"elsewhere.")
    else:
        agent_part = f"{n} credential(s) loaded from {TOKENS}"
    if c == 0:
        client_part = (f"WARNING: 0 approved-client credentials loaded from "
                       f"{CLIENT_TOKENS} — every human-reserved call (signal "
                       f"ack/unack, policy write) will be refused. Check the "
                       f"file exists and is readable by this user, or set "
                       f"LIBRARY_CLIENT_TOKENS.")
    else:
        client_part = f"{c} approved-client credential(s) loaded from {CLIENT_TOKENS}"
    dead = _dead_unknown_withholds(client_tokens)
    if dead:
        client_part += (f"; {len(dead)} withholding(s) name a capability no "
                        f"role defines, ignored for authorization: " +
                        "; ".join(dead))
    return f"{agent_part}; {client_part}"


def _dead_unknown_withholds(client_tokens: dict) -> list[str]:
    """One line per (entry, capability) where a client entry's `withholds`
    names a capability no role defines, for the boot log.

    **Logged once, here, rather than wherever `_entry_capabilities` resolves
    an entry** — that runs on every request (`_client_tokens` is re-read per
    request, same as `_tokens`), and this is called only from
    `describe_credentials`, which `app.lifespan` calls once at startup. The
    same information also reaches `dead_withholds` in `credential_report`, for
    a caller that wants it per request rather than at boot.

    Reads `meta` directly rather than going through `_entry_capabilities`:
    `client_tokens` is already the validated return of `_client_tokens()`, so
    every `withholds` here is already proved a list of strings.
    """
    known = _known_capabilities()
    out = []
    for token, meta in client_tokens.items():
        if not isinstance(meta, dict) or not meta:
            continue
        label = _entry_label(token, meta)
        for cap in meta.get("withholds", []):
            if cap not in known:
                out.append(f"{label}: {cap!r}")
    return out


def _tokens() -> dict:
    # RE-READ PER REQUEST, so a granted or revoked token takes effect with no
    # restart — the same no-restart ethos Orient's roles.json has. The fail-closed
    # catch below means a partially-written file refuses everyone rather than
    # admitting the wrong identity, so the update race is safe.
    #
    # Worth retiring at Phase 5 rather than carrying into a higher-traffic service:
    # `POST /tokens/redeem` replaces the file with tokens the Library itself
    # minted, and the read goes away with it.
    #
    # **VALID JSON OF THE WRONG SHAPE FAILS CLOSED TOO** (security-reviewer,
    # PR #49 round 3). A file whose top level is an array rather than an object
    # parses fine, so the catch below never fires — and every `.get()` against
    # it then raises `AttributeError`, taking auth down for EVERY token with a
    # 500 rather than the clean 401 an unreadable file already produces. One
    # hand-edit of that file could do it. The shape check makes a wrong-shaped
    # file behave exactly like an unreadable one.
    try:
        loaded = json.loads(TOKENS.read_text())
    except (OSError, ValueError):
        # FAIL CLOSED. An unreadable token file means nobody authenticates, which
        # takes the Library offline; the alternative is that a corrupted or
        # truncated file silently admits everyone. During the shadow an offline
        # Library costs nothing, because Orient is still serving.
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _client_tokens() -> dict:
    """The approved-client credentials, re-read per request for the same reason
    `_tokens()` is. It is its own file, so a broken client file cannot disturb
    ordinary agent auth: it zeroes out approved clients only, refusing every
    human-reserved call, while `_tokens()` keeps resolving `agent` and `system`
    exactly as before.

    **THREE CASES, AND ONLY THE LAST IS FATAL.**

    - *A file that is missing, unreadable, not JSON, or not an object* is an
      empty store, as it has always been (`tests/test_customer_reader_role.py`
      pins the shape guard). That fails closed in the safe direction: no
      approved client authenticates and none is widened.
    - *A file whose entries are all resolvable* is returned.
    - *A file holding an entry whose `grants`/`holds_reserved` cannot be
      resolved* (a `grants` entry naming a capability no role defines, a
      human-reserved capability in `grants`, wrong shape) raises
      `CredentialStoreError`, and the Library then serves no request
      (`app.refuse_on_unresolvable_credentials`). An unknown capability in
      `withholds` is the one exception (`_entry_capabilities`): it resolves
      rather than raising, because this store is fatal to the whole service
      on any entry it cannot resolve, and a `withholds` entry can go stale
      the moment a role stops defining a name it once held. Skipping an
      unresolvable entry outright, for anything still fatal here, would be
      the one unsafe answer: a withholding dropped as unreadable silently
      widens exactly the credential it narrows. This file is the Library's
      own, read by this process alone, so refusing all of it over anything
      still fatal costs nothing a stranger can trigger.

    **The opposite is right for `api-tokens.json`** (`resolve`, below), which
    this repo does not own — Orient's tooling writes it, on macstudio today — so
    there one bad entry loses that entry only.

    Entries that are not non-empty objects are still skipped rather than
    refused, as `resolve` has always done.

    **Known, and left unguarded: a token present in BOTH credential files.**
    When this file is unreadable the token resolves through `_tokens()` as the
    `agent` role instead, which can regain a capability its client entry
    withheld. Nothing shows that case occurring, so there is no guard.
    """
    try:
        loaded = json.loads(CLIENT_TOKENS.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    for token, meta in loaded.items():
        if isinstance(meta, dict) and meta:
            try:
                label = _entry_label(token, meta)
                _, withholds = _entry_capabilities(meta, "approved_client",
                                                   label)
                _entry_holds_reserved(meta, "approved_client", label, withholds)
            except _BadEntry as exc:
                raise CredentialStoreError(str(exc))
    return loaded


def credential_store_error() -> str | None:
    """Why the client store cannot be resolved right now, or None."""
    try:
        _client_tokens()
    except CredentialStoreError as exc:
        return str(exc)
    return None


def _safe_actor(raw) -> str:
    """The `name` field, proved to be a string.

    **`actor` reaches SQLite's parameter binding on roughly twenty write paths**
    (security-reviewer, PR #49). A JSON object or array here does not fail at
    authentication, where it would be legible — it fails on the first
    authenticated WRITE that token attempts, as `sqlite3.InterfaceError:
    unsupported type`, which reads as a database fault rather than a malformed
    credential.

    Falls back to the same `"unknown"` the missing-key case already used, rather
    than stringifying: `str({"a": 1})` would mint an actor named `{'a': 1}` and
    write it into an audit column, which is a worse record than admitting the
    name could not be read.
    """
    return raw if isinstance(raw, str) else "unknown"


def _safe_products(raw) -> frozenset[str]:
    """The `products` field, proved to be a list of strings.

    **TWO DEFECTS, AND THE QUIET ONE IS WORSE** (security-reviewer, PR #49).

    `frozenset(5)`, `frozenset(None)` and `frozenset(True)` all raise
    `TypeError: not iterable`, uncaught on the auth path — a 500 on every
    request that token presents, where the docstring promises a fail-closed
    refusal.

    And `frozenset("reports")` is `{'r','e','p','o','t','s'}`, not
    `{'reports'}`. A `"products": "reports"` typo — a plausible one, since every
    other value in these files is a bare string — silently scopes the token to
    six single-letter products. `in_scope(ident, "reports")` then returns False,
    denying the product actually intended, while `in_scope(ident, "r")` returns
    True, granting one nobody meant to grant. It fails in both directions at
    once and reports neither.

    **Scoped to nothing on anything malformed**, which `_scope_clause` renders as
    `1 = 0`: a credential whose scope cannot be read sees no product, rather than
    seeing a guess. Non-string members are dropped individually for the same
    reason — a list holding one bad entry still yields the entries that ARE
    readable, since discarding them all would turn a typo in one product name
    into a total outage for that token.
    """
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(p for p in raw if isinstance(p, str))


def resolve(authorization: str | None) -> Identity | None:
    """A bearer token to an identity, or None when it does not authenticate.

    **The client file is consulted first, and a token present in both files
    resolves as `approved_client`.** The alternative ordering would silently
    downgrade a client to an agent whenever the same token also happened to sit
    in the agent/system file — the client authenticates fine and simply cannot do
    the one thing it exists to do, and the symptom is a 403 nobody can explain.

    Past the client file, a token's `kind` resolves to itself when `NAMEABLE_KINDS`
    recognises it; **anything else resolves as agent**, including a kind value
    nobody recognises. That is deliberate fail-closed behaviour: a typo in the
    token file must not mint a role with more capability than the one intended,
    and `agent` is the least of them.

    **`approved_client` is deliberately not nameable here**, whatever a token
    file says. It comes only from the separate client file consulted above, which
    is the whole shape ADR-0038 draws.
    """
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[len("Bearer "):].strip()
    # **THE ENTRY'S SHAPE, NOT ONLY ITS FIELDS** (security-reviewer, PR #49
    # round 3). `{"tok": "oops"}` is valid JSON; a truthy non-dict passes the
    # old `if client_meta:` and then raises `AttributeError` on `.get`, which is
    # a 500 where a malformed credential should be a 401. Guarding the fields
    # inside an entry does nothing if the entry is not a mapping at all.
    client_meta = _client_tokens().get(token)
    if isinstance(client_meta, dict) and client_meta:
        # Already validated by `_client_tokens()` above, so this cannot raise
        # `_BadEntry`; a failure to resolve THIS file never reaches here.
        label = _entry_label(token, client_meta)
        grants, withholds = _entry_capabilities(
            client_meta, "approved_client", label)
        return Identity(actor=_safe_actor(client_meta.get("name", "unknown")),
                        kind="approved_client",
                        products=_safe_products(client_meta.get("products", [])),
                        grants=grants, withholds=withholds,
                        holds_reserved=_entry_holds_reserved(
                            client_meta, "approved_client", label, withholds))
    meta = _tokens().get(token)
    if not isinstance(meta, dict) or not meta:
        # A non-dict entry is a malformed credential, and a malformed credential
        # authenticates nobody — the same answer an absent one already gets.
        return None
    # **TYPE-CHECKED BEFORE THE MEMBERSHIP TEST** (security-reviewer, PR #49).
    # `in` against a frozenset HASHES the probe, so a `kind` holding a JSON list
    # or object raises `TypeError: unhashable type` — unhandled, on the auth
    # path, which is a 500 on every request that token presents rather than the
    # fail-closed fallback three lines of docstring above promise.
    #
    # The old `== "system"` comparison could not do this; swapping it for a
    # membership test is what reintroduced the class. It is the third time in
    # this service: `privacy_class` reached a frozenset the same way (PR #4) and
    # so did `kind`/`project`/`status`/`visibility` in `reference_admission`
    # (PR #42). The lesson that keeps not sticking is that `in` against a set is
    # a hash, and a caller-shaped value must be proved a string first.
    #
    # `api-tokens.json` is written by whatever mints credentials for this
    # deploy — Orient's tooling, on macstudio today — and this repo does not own
    # what lands in it, so a malformed entry is data this service must
    # survive rather than trust — which is why `name` and `products`
    # take the same treatment through `_safe_actor` and `_safe_products`
    # above, rather than this fix reaching only the field that was
    # reported.
    claimed = meta.get("kind")
    kind = (claimed if isinstance(claimed, str) and claimed in NAMEABLE_KINDS
            else "agent")
    # **A BAD CAPABILITY FIELD HERE REFUSES THE ENTRY, AND NEVER THE SERVICE.**
    # The client store above is fatal to serving because this process alone
    # writes it. This file is not — anyone who can write it, Orient's tooling
    # on macstudio today, could otherwise halt the Library with one bad entry,
    # which is the reason `_safe_actor` and `_safe_products` degrade per entry
    # too. The entry authenticates nobody (401, the answer any malformed
    # credential gets), the log names it, and every other token keeps
    # resolving.
    try:
        label = _entry_label(token, meta)
        grants, withholds = _entry_capabilities(meta, kind, label)
        # Refuses the field on this kind; it is meaningful only on a client.
        _entry_holds_reserved(meta, kind, label, withholds)
    except _BadEntry as exc:
        _log(f"REFUSED a credential in {TOKENS}: {exc}")
        return None
    return Identity(actor=_safe_actor(meta.get("name", "unknown")), kind=kind,
                    products=_safe_products(meta.get("products", [])),
                    grants=grants, withholds=withholds)


def authorized(ident: Identity, capability: str) -> bool:
    return capability in resolve_capabilities(ident).caps


def capability_report(ident: Identity) -> dict:
    """What a credential resolves to and where each capability comes from."""
    r = resolve_capabilities(ident)
    return {"role": r.role, "capabilities": sorted(r.caps),
            "from_role": sorted(r.from_role), "from_entry": sorted(r.from_entry),
            "withheld_by_entry": sorted(r.withheld),
            # Reserved capabilities the entry's `holds_reserved` did not list.
            "excluded_by_holds_reserved": sorted(r.excluded_by_holds_reserved),
            # Surfaced so a withholding the role does not yet give is visible
            # before a role change makes it bite.
            "dead_withholds": sorted(r.dead_withholds)}


def credential_report() -> list[dict]:
    """`capability_report` for every credential in both stores, keyed by the
    entry's name and a token hash and never by the token. Read-only."""
    out = []
    client_entries = _client_tokens()
    for source, path, entries in (("client", CLIENT_TOKENS, client_entries),
                                  ("agent", TOKENS, _tokens())):
        for token, meta in entries.items():
            if not isinstance(meta, dict) or not meta:
                continue
            if source == "agent" and token in client_entries:
                continue  # resolves as a client; its client row says so
            ident = resolve(f"Bearer {token}")
            row = {"store": source, "path": str(path),
                   "entry": _entry_label(token, meta)}
            row.update(capability_report(ident) if ident
                       else {"refused": "entry does not resolve"})
            out.append(row)
    return out


if __name__ == "__main__":
    print(json.dumps(credential_report(), indent=2))


def attested_human(ident: Identity, payload: dict) -> tuple[str | None, dict | None]:
    """The human a human-reserved route should record as the actor, or a refusal
    body in the existing refusal shape — the route decides the status code.

    Refuses unless `ident.kind == "approved_client"`. In practice a caller reaches
    this only after the route's own capability check (`signal.ack`, say) has
    already passed, which only an approved client can hold — so this is a second,
    independent gate on the same fact rather than the only one, the same
    belt-and-suspenders shape `insert_signal` uses on `privacy_class`.

    Requires a non-empty `acting_human` on the request body, bounded by
    `store.MAX_ACTOR`. **The caller must never fall back to `ident.actor` when
    this is absent or refused** — that would record the client as having acted,
    which is exactly the identity ADR-0038 draws the line to keep out of this
    field.
    """
    if ident.kind != "approved_client":
        return None, {"error": "acting_human is recorded only for an "
                                "approved-client credential"}
    acting_human = payload.get("acting_human") if isinstance(payload, dict) else None
    if not isinstance(acting_human, str) or not acting_human.strip():
        return None, {"error": "acting_human is required"}
    trimmed = acting_human.strip()
    if len(trimmed) > store.MAX_ACTOR:
        return None, {"error": f"acting_human is longer than {store.MAX_ACTOR} "
                                f"characters"}
    return trimmed, None


def in_scope(ident: Identity, product: str | None) -> bool:
    """Product scoping. `"*"` sees everything; a row with no product is visible to
    a wildcard identity only, since there is no scope it could be checked against
    and guessing would be a leak."""
    if "*" in ident.products:
        return True
    if product is None:
        return False
    return product in ident.products


# `ledger.grant` SETS A SCOPE'S CAP SET, AND IT IS ITS OWN LITERAL ENTRY IN
# THE APPROVED-CLIENT UNION ABOVE, for `policy.write`'s reason rather than by
# analogy to it.
#
# **The operator's call, 2026-08-23: a cap change is a direct human change made through
# Orient.** Spending accumulates a ledger's total and setting caps decides what
# refuses it, and only the second is reserved — `ledger.spend` sits in `agent`
# precisely because `agentctl` streams spend continuously and must never need a
# human's credential to report what a run already cost.
#
# Reserving the cap set is what stops an agent raising its own ceiling. Without
# it the ledger enforces nothing: a worker refused at checkout for a capped
# ledger could raise its own cap and check out again, which is the budget
# equivalent of the connector deny-list's self-widening risk and the same
# boundary answers both.
#
# **`ledger.void` sits beside it for the same reason, added when voiding was
# built** (`issue_01M0B8YCFN4C1CYNANVG87CANG`). Voiding corrects the record
# that gates admission, so an agent able to void its own spend could spend past
# a cap and then erase the evidence — the same "a gate must never trust a field
# its subject controls" rule the ledger's day key already follows, applied to
# the log rather than to one column of it.
#
# **The consequence is deliberate and worth stating, because it is an
# operational cost rather than a free win.** `approved_client` is not a
# `NAMEABLE_KINDS` value, so no token file can mint one — the credential exists
# once, as Orient's. A cap change is therefore settable only by a human through
# Orient's surface, and not from a co session, from `agentctl`, or by any agent
# including one acting on the operator's instruction.
#
# As with `policy.write`, the capability is not the whole control: the write
# route ALSO refuses unconditionally on `ident.kind != "approved_client"`, and
# records the acting human from `attested_human` rather than from `ident.actor`,
# so the change is attributed to the person rather than to the client that
# carried it.
