"""Whether a Reference Room document change may land (ADR-0022,
`issue_01M08YECXZ68NG1WWHG0NRD086`) — the frontmatter and checkout clauses,
both evaluated on every request.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import projects
import protocol
import reference_admission
import store

AGENT = "agent-token-vivy"
SYSTEM = "system-token"


@pytest.fixture
def client(tmp_path, monkeypatch):
    tokens = tmp_path / "api-tokens.json"
    tokens.write_text('{"%s": {"name": "vivy-agent", "products": ["vivy"]},'
                      ' "%s": {"name": "capture", "products": ["*"], '
                      '"kind": "system"}}' % (AGENT, SYSTEM))
    monkeypatch.setattr(identity, "TOKENS", tokens)
    return TestClient(libapp.app)


def _h(token):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def _fm(**over):
    out = {"id": "library/spec/example", "kind": "spec", "project": "library",
           "title": "An example", "status": "draft"}
    out.update(over)
    return out


# --- validate_frontmatter, the pure function ---------------------------------


def test_a_valid_frontmatter_has_no_errors():
    assert reference_admission.validate_frontmatter(_fm()) == []


def test_not_a_mapping():
    assert reference_admission.validate_frontmatter("not a dict") == [
        "frontmatter is not a JSON object"]


@pytest.mark.parametrize("field", ["id", "kind", "project", "title", "status"])
def test_missing_required_field(field):
    fm = _fm()
    del fm[field]
    errs = reference_admission.validate_frontmatter(fm)
    assert any(field in e for e in errs)


@pytest.mark.parametrize("field", ["id", "kind", "project", "title", "status"])
def test_empty_required_field(field):
    errs = reference_admission.validate_frontmatter(_fm(**{field: ""}))
    assert any(field in e for e in errs)


def test_invalid_kind():
    errs = reference_admission.validate_frontmatter(_fm(kind="not-a-kind"))
    assert any("invalid kind" in e for e in errs)


def test_invalid_project():
    errs = reference_admission.validate_frontmatter(_fm(project="not-a-project"))
    assert any("invalid project" in e for e in errs)


def test_invalid_status():
    errs = reference_admission.validate_frontmatter(_fm(status="not-a-status"))
    assert any("invalid status" in e for e in errs)


def test_invalid_visibility():
    errs = reference_admission.validate_frontmatter(_fm(visibility="not-a-visibility"))
    assert any("invalid visibility" in e for e in errs)


@pytest.mark.parametrize("field", ["kind", "project", "status", "visibility"])
@pytest.mark.parametrize("value", [["a", "list"], {"a": "dict"}, 12345, True])
def test_non_string_scalar_field_refuses_cleanly_rather_than_500ing(field, value):
    """`in` against a frozenset hashes the probe — an unhashable value (a
    list or dict) must return a validation error, not raise `TypeError`
    (security-reviewer, PR #42)."""
    errs = reference_admission.validate_frontmatter(_fm(**{field: value}))
    assert any(f"invalid {field}" in e for e in errs)


def test_visibility_absent_is_fine():
    assert reference_admission.validate_frontmatter(_fm()) == []


def test_invalid_id_format():
    errs = reference_admission.validate_frontmatter(_fm(id="not-a-valid-id"))
    assert any("invalid id format" in e for e in errs)


@pytest.mark.parametrize("field", ["topics", "audience", "applies_to",
                                   "depends_on", "authors", "tools",
                                   "binds_crews", "binds_agents", "binds_topics",
                                   "binds_sessions", "binds_roles"])
def test_list_field_must_be_a_list(field):
    errs = reference_admission.validate_frontmatter(_fm(**{field: "not-a-list"}))
    assert any(f"{field} must be a list" in e for e in errs)


def test_list_field_as_a_real_list_is_fine():
    assert reference_admission.validate_frontmatter(
        _fm(topics=["a", "b"], authors=["operator"])) == []


def test_last_reviewed_valid_date():
    assert reference_admission.validate_frontmatter(
        _fm(last_reviewed="2026-08-19")) == []


def test_last_reviewed_invalid_format():
    errs = reference_admission.validate_frontmatter(_fm(last_reviewed="08/19/2026"))
    assert any("last_reviewed must be YYYY-MM-DD" in e for e in errs)


def test_last_reviewed_absent_is_fine():
    assert reference_admission.validate_frontmatter(_fm()) == []


def test_multiple_errors_all_reported():
    errs = reference_admission.validate_frontmatter(
        {"kind": "not-a-kind", "project": "not-a-project"})
    # id, title, status missing + invalid kind + invalid project = 5
    assert len(errs) == 5


# --- the knowledge-tree-derived project catalogue (agent-platform#685) ---------
#
# `ALLOWED_PROJECTS`/`ID_PATTERN` are the baseline; `project_catalogue` widens
# them with whatever `<root>/projects/` actually holds, so an adopter's own
# project — one Convivy's static list has never heard of — validates.


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """A synthetic `agent-platform/config/projects.json` `projects.private_repos()`
    reads, so a private-crew refusal can be tested without the real registry.

    **NEVER PASS `{}`.** `projects.private_repos()` treats an empty dict the
    same as a missing file — `if not raw: return None` — so a genuinely empty
    registry reads as unreadable and every non-baseline name gets refused for
    the wrong reason. Pass a harmless filler entry (`{"filler": {}}`) for "no
    private crews today"; a real unreadable-registry case points
    `REGISTRY_PATH` at a file that does not exist instead of calling this."""
    def _set(entries):
        path = tmp_path / "projects.json"
        path.write_text(json.dumps(entries))
        monkeypatch.setattr(projects, "REGISTRY_PATH", path)
        projects.reload()
    yield _set
    # Clears the cache while `monkeypatch` still has `REGISTRY_PATH` pointed at
    # this fixture's tmp_path, so the next test's first read repopulates it from
    # the real registry `monkeypatch` restores immediately after (the same
    # ordering `test_item_writes.py`'s own registry fixture relies on).
    projects.reload()


def test_a_project_directory_not_in_the_baseline_is_admitted(tmp_path, registry):
    registry({"filler": {}})
    assert "adopter-app" not in reference_admission.ALLOWED_PROJECTS
    (tmp_path / "projects" / "adopter-app").mkdir(parents=True)

    cat = reference_admission.project_catalogue(tmp_path)
    assert "adopter-app" in cat.allowed
    assert cat.id_pattern.match("adopter-app/reference/x")
    fm = {"id": "adopter-app/reference/x", "kind": "reference",
          "project": "adopter-app", "title": "A doc", "status": "draft"}
    assert reference_admission.validate_frontmatter(fm, cat) == []
    # and without a catalogue, the baseline alone still refuses it
    assert reference_admission.validate_frontmatter(fm) != []


def test_a_missing_project_directory_is_refused(tmp_path, registry):
    registry({"filler": {}})
    (tmp_path / "projects").mkdir(parents=True)
    cat = reference_admission.project_catalogue(tmp_path)
    assert "nowhere" not in cat.allowed
    assert reference_admission.project_dir_refusal(tmp_path, "nowhere") == (
        "'nowhere' is not a directory")
    fm = {"id": "nowhere/reference/x", "kind": "reference", "project": "nowhere",
          "title": "A doc", "status": "draft"}
    errs = reference_admission.validate_frontmatter(fm, cat)
    assert any("invalid project: 'nowhere'" in e for e in errs), errs


def test_root_unset_admits_only_the_baseline(registry):
    """`KNOWLEDGE_ROOT` unset is today's behaviour — deploying this code ahead
    of the plist change that sets it must not widen anything, including
    through `pending`."""
    registry({"filler": {}})
    cat = reference_admission.project_catalogue(None, pending="brand-new")
    assert cat.allowed == reference_admission.ALLOWED_PROJECTS
    assert cat.id_pattern.pattern == reference_admission.ID_PATTERN.pattern
    assert "brand-new" not in cat.allowed


def test_an_unreadable_projects_directory_admits_only_the_baseline(tmp_path, registry):
    registry({"filler": {}})
    root = tmp_path / "tree"
    root.mkdir()
    # No `projects/` directory at all under a real root: `iterdir` raises OSError.
    cat = reference_admission.project_catalogue(root)
    assert cat.allowed == reference_admission.ALLOWED_PROJECTS


@pytest.mark.parametrize("scope", ["shared", "convivy", "utility", "engineering"])
def test_a_retired_name_is_refused(tmp_path, registry, scope):
    registry({"filler": {}})
    (tmp_path / "projects" / scope).mkdir(parents=True)
    cat = reference_admission.project_catalogue(tmp_path)
    assert scope not in cat.allowed
    assert "retired scope" in cat.refused[scope]


def test_a_private_crew_name_is_refused(tmp_path, registry):
    registry({"secretproject": {"private": True, "repos": ["secret-repo"]}})
    (tmp_path / "projects" / "secretproject").mkdir(parents=True)
    cat = reference_admission.project_catalogue(tmp_path)
    assert "secretproject" not in cat.allowed
    assert "private crew" in cat.refused["secretproject"]
    assert "0043" in cat.refused["secretproject"]


# `_name_refusal` must refuse every name a private crew answers to, not just
# its slug and `repos` (`projects.private_repos()`'s set) — an alias, a
# `display` name, a `crew` name and a `home_dir`/`dirs` basename each reach
# `project_catalogue`'s `pending` path with no directory on disk to key a
# refusal off, so each is exercised through `pending` rather than a mkdir'd
# `projects/<name>` (reviewer finding, PR #160).


def test_a_private_crews_alias_is_refused(tmp_path, registry):
    registry({"secretproject": {"private": True, "aliases": ["secret-alias"]}})
    (tmp_path / "projects").mkdir(parents=True)
    cat = reference_admission.project_catalogue(tmp_path, pending="secret-alias")
    assert "secret-alias" not in cat.allowed
    assert "private crew" in cat.refused["secret-alias"]


def test_a_private_crews_display_name_is_refused(tmp_path, registry):
    registry({"secretproject": {"private": True, "display": "Secret-Display"}})
    (tmp_path / "projects").mkdir(parents=True)
    cat = reference_admission.project_catalogue(tmp_path, pending="secret-display")
    assert "secret-display" not in cat.allowed
    assert "private crew" in cat.refused["secret-display"]


def test_a_private_crews_crew_name_is_refused(tmp_path, registry):
    registry({"secretproject": {"private": True, "crew": "secret-crew"}})
    (tmp_path / "projects").mkdir(parents=True)
    cat = reference_admission.project_catalogue(tmp_path, pending="secret-crew")
    assert "secret-crew" not in cat.allowed
    assert "private crew" in cat.refused["secret-crew"]


def test_a_private_crews_home_dir_basename_is_refused(tmp_path, registry):
    registry({"secretproject": {"private": True,
                                 "home_dir": str(tmp_path / "Convivy" / "secret-home")}})
    (tmp_path / "projects").mkdir(parents=True)
    cat = reference_admission.project_catalogue(tmp_path, pending="secret-home")
    assert "secret-home" not in cat.allowed
    assert "private crew" in cat.refused["secret-home"]


def test_a_truthy_non_true_private_value_does_not_count(tmp_path, registry):
    """`private: "true"` (a string, not the boolean) does not mark a crew
    private, matching agent-platform#685, which drew the same line."""
    registry({"almostprivate": {"private": "true", "aliases": ["not-really-secret"]}})
    (tmp_path / "projects").mkdir(parents=True)
    cat = reference_admission.project_catalogue(tmp_path, pending="not-really-secret")
    assert "not-really-secret" in cat.allowed


@pytest.mark.parametrize("pending", ["secretproject", "SecretProject", "shared"])
def test_a_pending_private_or_retired_name_is_refused(tmp_path, registry, pending):
    """Mirrors agent-platform#685's
    `test_a_private_crews_first_document_fails_the_write_precheck`: a caller
    cannot use the pending-write path to assert a private or retired name a
    directory listing would have refused."""
    registry({"secretproject": {"private": True, "repos": ["secret-repo"]}})
    (tmp_path / "projects").mkdir(parents=True)
    cat = reference_admission.project_catalogue(tmp_path, pending=pending)
    assert pending not in cat.allowed


@pytest.mark.parametrize("name", ["Upper", ".hidden", "under_score", "9lives"])
def test_a_bad_slug_is_refused(tmp_path, registry, name):
    registry({"filler": {}})
    (tmp_path / "projects" / name).mkdir(parents=True)
    cat = reference_admission.project_catalogue(tmp_path)
    assert name not in cat.allowed
    assert "not a plain lowercase directory name" in cat.refused[name]


def test_an_escaping_symlink_is_refused(tmp_path, registry):
    registry({"filler": {}})
    root = tmp_path / "tree"
    (root / "projects").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "projects" / "escapee").symlink_to(outside)
    cat = reference_admission.project_catalogue(root)
    assert "escapee" not in cat.allowed
    assert "outside the knowledge tree" in cat.refused["escapee"]


def test_a_pending_project_is_admitted_by_name(tmp_path, registry):
    """The project a write is about to create is admitted before its
    directory exists — first-save-of-a-new-project has no directory yet."""
    registry({"filler": {}})
    (tmp_path / "projects").mkdir(parents=True)
    assert not (tmp_path / "projects" / "brand-new").exists()
    cat = reference_admission.project_catalogue(tmp_path, pending="brand-new")
    assert "brand-new" in cat.allowed
    assert cat.id_pattern.match("brand-new/reference/x")


def test_an_unreadable_private_crew_source_refuses_non_baseline_names(tmp_path, monkeypatch, registry):
    monkeypatch.setattr(projects, "REGISTRY_PATH", tmp_path / "no-such-file.json")
    projects.reload()
    assert projects.private_repos() is None

    (tmp_path / "projects" / "newproj").mkdir(parents=True)
    cat = reference_admission.project_catalogue(tmp_path, pending="also-new")
    assert "newproj" not in cat.allowed
    assert "could not be read" in cat.refused["newproj"]
    assert "also-new" not in cat.allowed
    assert "could not be read" in cat.refused["also-new"]
    # a baseline project still validates, so an outage does not drop the corpus
    assert "library" in cat.allowed


# --- the route -----------------------------------------------------------------


@pytest.mark.parametrize("token", [AGENT, SYSTEM])
def test_every_identity_can_ask(client, held, token):
    held("library/spec/example", "operator")
    r = client.post("/reference/admission",
                    json={"frontmatter": _fm(), "owner": "operator"},
                    headers=_h(token))
    assert r.status_code == 200
    body = r.json()
    # Keys rather than whole-response equality: the protocol states that
    # within a version the Library only ADDS, and that clients must ignore
    # response fields they do not recognise. A test asserting an exact
    # response body forbids the one kind of change the contract allows.
    assert body["may_land"] is True and body["errors"] == []


def test_unauthenticated_is_refused(client):
    r = client.post("/reference/admission", json={"frontmatter": _fm()},
                    headers={protocol.HEADER: str(protocol.CURRENT)})
    assert r.status_code == 401


def test_invalid_frontmatter_reports_may_land_false(client):
    r = client.post("/reference/admission",
                    json={"frontmatter": _fm(kind="not-a-kind")},
                    headers=_h(AGENT))
    assert r.status_code == 200
    body = r.json()
    assert body["may_land"] is False
    assert any("invalid kind" in e for e in body["errors"])


def test_a_list_shaped_kind_does_not_500_through_the_route(client):
    """The end-to-end version of `test_non_string_scalar_field_refuses_
    cleanly_rather_than_500ing` — through the real HTTP path rather than the
    pure function directly (security-reviewer, PR #42)."""
    r = client.post("/reference/admission",
                    json={"frontmatter": _fm(kind=["spec"])},
                    headers=_h(AGENT))
    assert r.status_code == 200
    body = r.json()
    assert body["may_land"] is False
    assert any("invalid kind" in e for e in body["errors"])


def test_frontmatter_is_required(client):
    r = client.post("/reference/admission", json={}, headers=_h(AGENT))
    assert r.status_code == 400
    assert "frontmatter" in r.json()["error"]


def test_non_dict_body_is_refused(client):
    r = client.post("/reference/admission", json=["not", "a", "dict"],
                    headers=_h(AGENT))
    assert r.status_code == 400


def test_non_dict_frontmatter_is_a_may_land_false_not_a_400(client):
    """A malformed frontmatter value is itself a validation finding, not a
    request-shape error — the caller asked a well-formed question and got a
    well-formed (negative) answer. Names an owner so the assertion isolates to
    the frontmatter clause: a non-dict frontmatter has no `id` to look up, so
    the checkout clause is left unrun regardless."""
    r = client.post("/reference/admission",
                    json={"frontmatter": "not a dict", "owner": "operator"},
                    headers=_h(AGENT))
    assert r.status_code == 200
    body = r.json()
    assert body["may_land"] is False
    assert body["errors"] == ["frontmatter is not a JSON object"]
    assert body["clauses_checked"] == ["frontmatter"]


# --- drift against knowledge/scripts/validate.py --------------------------------


def test_the_validation_constants_have_not_drifted():
    """Two validators over one schema disagreeing would make "does this land"
    depend on which door you came through — the same shape
    `test_the_ranking_constants_have_not_drifted` already guards for the BM25
    constants.

    **Two halves, and both run in CI.** The literals below fail the moment
    THIS copy changes. The cross-repo comparison reads `scripts/validate.py`
    from the knowledge checkout `KNOWLEDGE_ROOT` names; CI checks
    `convivy/knowledge` out beside this repo and sets it. When the variable is
    set and `validate.py` is not there, the test FAILS, so a broken checkout
    step cannot turn back into a silent skip. It skips only when neither the
    variable nor `~/Convivy/knowledge` exists (a machine with no knowledge
    checkout).
    """
    assert reference_admission.ALLOWED_STATUSES == {
        "draft", "current", "deprecated", "archived"}
    assert reference_admission.ALLOWED_VISIBILITY == {"public", "team", "internal"}
    assert reference_admission.REQUIRED == (
        "id", "kind", "project", "title", "status")
    assert reference_admission.LIST_FIELDS == (
        "topics", "audience", "applies_to", "depends_on", "authors", "tools",
        "binds_crews", "binds_agents", "binds_topics", "binds_sessions",
        "binds_roles")
    assert reference_admission.ISO_DATE.pattern == r"^\d{4}-\d{2}-\d{2}$"
    assert "bead" in reference_admission.ALLOWED_KINDS
    assert "instruction-block" in reference_admission.ALLOWED_KINDS

    configured = os.environ.get("KNOWLEDGE_ROOT")
    if configured:
        source = pathlib.Path(configured) / "scripts/validate.py"
        assert source.is_file(), (
            f"KNOWLEDGE_ROOT is set to {configured!r} but {source} does not "
            "exist; the checkout step is broken, and skipping here would hide it")
    else:
        source = pathlib.Path.home() / "Convivy/knowledge/scripts/validate.py"
        if not source.is_file():
            pytest.skip("knowledge not checked out; the cross-repo half cannot run")
    text = source.read_text()

    # **EVERY CONSTANT IS COMPARED BEFORE ANYTHING FAILS, and the failure
    # names all of them.** This guard's job is to enumerate a multi-site
    # seam, and a chain of bare asserts stops at the first mismatch — so it
    # reports one drifted site while others are equally drifted, and it
    # under-counts exactly when someone is relying on it to tell them how
    # many sites to fix. That happened on 2026-09-06: `placard` was missing
    # from both `ALLOWED_PROJECTS` and `ID_PATTERN`, the projects assertion
    # failed first, and the work item was written up as a one-line fix
    # because the pattern's drift was still hidden behind it.
    drifted = []

    def compare(name, found, ours):
        """Records a mismatch; for sets it records the DIFFERENCE rather than
        both memberships. `ALLOWED_PROJECTS` holds two dozen entries, so
        printing both sides in full buries the one entry that moved in fifty
        names of noise — the reader has to diff two unordered dumps by eye to
        learn what this already knows."""
        if found == ours:
            return
        if isinstance(found, set) and isinstance(ours, set):
            parts = []
            if found - ours:
                parts.append(f"missing here: {sorted(found - ours)}")
            if ours - found:
                parts.append(f"not in validate.py: {sorted(ours - found)}")
            drifted.append(f"  {name}: " + "; ".join(parts))
        else:
            drifted.append(f"  {name}: validate.py has {found!r}, "
                           f"reference_admission has {ours!r}")

    kinds = re.search(r"ALLOWED_KINDS = \{(.*?)\n\}", text, re.S)
    assert kinds, "could not find ALLOWED_KINDS in validate.py"
    compare("ALLOWED_KINDS",
            set(re.findall(r'"([a-z-]+)"', kinds.group(1))),
            set(reference_admission.ALLOWED_KINDS))

    # **PROJECTS ARE THE MOST ACTIVELY-CHANGING CONSTANT IN THE SOURCE FILE**
    # (reviewer, PR #42) — new projects are added regularly, so this is
    # exactly the copy most likely to actually drift, and the one a
    # presence-only check (`"library" in ...`) does not catch: removing an
    # unrelated entry from the port's own copy left that assertion passing.
    # Extracted the same way `ALLOWED_KINDS` is, since the block is a `{...}`
    # set literal with no nested braces for a non-greedy match to stop at
    # early — confirmed against the live source rather than assumed.
    projects = re.search(r"ALLOWED_PROJECTS = \{(.*?)\}", text, re.S)
    assert projects, "could not find ALLOWED_PROJECTS in validate.py"
    compare("ALLOWED_PROJECTS",
            set(re.findall(r'"([a-z-]+)"', projects.group(1))),
            set(reference_admission.ALLOWED_PROJECTS))

    statuses = re.search(r"ALLOWED_STATUSES = \{([^}]+)\}", text)
    assert statuses, "could not find ALLOWED_STATUSES in validate.py"
    compare("ALLOWED_STATUSES",
            set(re.findall(r'"(\w+)"', statuses.group(1))),
            set(reference_admission.ALLOWED_STATUSES))

    visibility = re.search(r"ALLOWED_VISIBILITY = \{([^}]+)\}", text)
    assert visibility, "could not find ALLOWED_VISIBILITY in validate.py"
    compare("ALLOWED_VISIBILITY",
            set(re.findall(r'"(\w+)"', visibility.group(1))),
            set(reference_admission.ALLOWED_VISIBILITY))

    required = re.search(r"REQUIRED = \(([^)]+)\)", text)
    assert required, "could not find REQUIRED in validate.py"
    compare("REQUIRED",
            tuple(re.findall(r'"(\w+)"', required.group(1))),
            reference_admission.REQUIRED)

    id_pattern = re.search(r"ID_PATTERN = re\.compile\(\s*(.*?)\)\n", text, re.S)
    assert id_pattern, "could not find ID_PATTERN in validate.py"
    # Compared by the SET OF PROJECT NAMES each pattern accepts, not by literal
    # text. `reference_admission.ID_PATTERN` is now DERIVED from
    # `ALLOWED_PROJECTS` (`id_pattern_for`, ported from agent-platform#685)
    # rather than a second hand-maintained literal, so its text differs from
    # validate.py's own (sorted order, `\Z` anchoring a trailing newline out
    # where validate.py's `$` lets it through) even when the two agree on
    # every project. What must still agree is which names each one admits.
    found_text = "".join(re.findall(r'r"([^"]*)"', id_pattern.group(1)))
    found_projects = set(re.match(r"\^\((.*?)\)/", found_text).group(1).split("|"))
    ours_projects = set(re.match(
        r"\^\((.*?)\)/", reference_admission.ID_PATTERN.pattern).group(1).split("|"))
    compare("ID_PATTERN projects", found_projects, ours_projects)

    list_fields = re.search(r"LIST_FIELDS = \(([^)]+)\)", text)
    assert list_fields, "could not find LIST_FIELDS in validate.py"
    compare("LIST_FIELDS",
            tuple(re.findall(r'"(\w+)"', list_fields.group(1))),
            reference_admission.LIST_FIELDS)

    iso_date = re.search(r'ISO_DATE = re\.compile\(r"([^"]+)"\)', text)
    assert iso_date, "could not find ISO_DATE in validate.py"
    compare("ISO_DATE",
            iso_date.group(1),
            reference_admission.ISO_DATE.pattern)

    assert not drifted, (
        f"{len(drifted)} constant(s) have drifted from "
        f"knowledge/scripts/validate.py:\n" + "\n".join(drifted))


# ---------------------------------------------------------------- the checkout clause


def test_the_clause_passes_when_the_owner_holds_the_checkout():
    assert reference_admission.check_checkout(
        "d/x", held_by="operator@example.com", owner="operator@example.com") == []


def test_the_clause_refuses_when_nobody_holds_a_checkout():
    errs = reference_admission.check_checkout("d/x", held_by=None, owner="operator")
    assert errs and "no live checkout" in errs[0]
    assert "d/x" in errs[0]


def test_the_clause_refuses_when_someone_else_holds_it_and_names_them():
    errs = reference_admission.check_checkout("d/x", held_by="ann", owner="operator")
    assert errs and "ann" in errs[0] and "operator" in errs[0]


@pytest.fixture
def held(tmp_path, monkeypatch):
    """A real `reference_checkouts` table, so the clause's lookup is exercised
    rather than mocked. Returns a function that takes a checkout on a doc."""
    db = tmp_path / "control.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE reference_checkouts (
            id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, holder TEXT NOT NULL,
            run_id TEXT, taken_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL,
            released_at TEXT, release_reason TEXT, released_by TEXT);
        CREATE UNIQUE INDEX ix_reference_checkouts_live_doc
            ON reference_checkouts (doc_id) WHERE released_at IS NULL;
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(store, "DB_PATH", db)

    def take(doc_id, holder, *, when=None):
        # The service's own clock and format, rather than a literal: a fixed
        # timestamp is already past the heartbeat grace by the time anyone runs
        # the suite, so a "live" checkout would read as dead and the admitting
        # half of the pair would fail for the wrong reason.
        if when is None:
            when = _dt.datetime.now(_dt.timezone.utc).replace(
                tzinfo=None).isoformat(" ")
        c = sqlite3.connect(db)
        c.execute("INSERT INTO reference_checkouts "
                  "(id, doc_id, holder, taken_at, heartbeat_at) "
                  "VALUES (?,?,?,?,?)", (f"co-{doc_id}", doc_id, holder, when, when))
        c.commit()
        c.close()
    return take


def test_omitting_owner_is_refused_naming_the_checkout_clause(client):
    """The checkout clause is unconditional now
    (issue_01M19QW42CFK7TDZCKWK18ECY5): a caller that does not name an owner
    is refused rather than answered on frontmatter alone, and the refusal
    names the checkout clause."""
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm()})
    assert r.status_code == 200
    body = r.json()
    assert body["may_land"] is False
    assert body["clauses_checked"] == ["frontmatter", "checkout"]
    assert body["clauses_refused"] == ["checkout"]
    assert any("owner" in e for e in body["errors"])


def test_naming_an_owner_who_holds_it_admits(client, held):
    """The admitting half of the pair. Identical to the refusing case below but
    for who holds the checkout, which is what proves the answer was read."""
    held("library/spec/example", "operator")
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(), "owner": "operator"})
    body = r.json()
    assert body["may_land"] is True
    assert body["clauses_checked"] == ["frontmatter", "checkout"]
    assert body["clauses_refused"] == []


def test_naming_an_owner_who_does_not_hold_it_refuses(client, held):
    held("library/spec/example", "ann")
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(), "owner": "operator"})
    body = r.json()
    assert body["may_land"] is False
    assert body["clauses_refused"] == ["checkout"]
    assert "ann" in body["errors"][0]


def test_an_unheld_document_refuses(client, held):
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(), "owner": "operator"})
    body = r.json()
    assert body["may_land"] is False
    assert body["clauses_refused"] == ["checkout"]
    assert "no live checkout" in body["errors"][0]


def test_a_dead_checkout_reads_as_unheld_rather_than_as_a_conflict(client, held):
    """An expired hold is the row still occupying the document, and the next
    writer is entitled to it — so it refuses for the absence of a checkout
    rather than naming the stale holder as a conflict."""
    held("library/spec/example", "ann", when="2020-01-01 00:00:00")
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(), "owner": "operator"})
    body = r.json()
    assert body["may_land"] is False
    assert "no live checkout" in body["errors"][0]
    assert "ann" not in body["errors"][0]


def test_a_document_failing_both_clauses_when_no_owner_is_named_is_refused_naming_both(client):
    """CLAUSE ISOLATION, and the reason it is not optional: a refusal case that
    fails BOTH clauses at once stays green against an implementation checking
    only one. Bad frontmatter and no owner fail both clauses at once — this
    used to be the omitted-owner clause-isolation case, before the checkout
    clause became unconditional and started failing here too."""
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(kind="not-a-kind")})
    body = r.json()
    assert body["may_land"] is False
    assert body["clauses_checked"] == ["frontmatter", "checkout"]
    assert body["clauses_refused"] == ["frontmatter", "checkout"]


def test_a_document_failing_both_clauses_when_the_owner_does_not_hold_it_is_refused_naming_both(client, held):
    """The other flavor of failing both clauses: an owner is named, so the
    checkout clause actually runs its lookup, and it refuses because someone
    else holds the checkout rather than because no owner was given."""
    held("library/spec/example", "ann")
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(kind="not-a-kind"), "owner": "operator"})
    body = r.json()
    assert body["may_land"] is False
    assert body["clauses_checked"] == ["frontmatter", "checkout"]
    assert body["clauses_refused"] == ["frontmatter", "checkout"]
    assert "ann" in body["errors"][-1]


def test_an_empty_id_with_an_owner_refuses_on_frontmatter(client):
    """Superseding an earlier version of this test that asserted a 400. The
    route no longer refuses the request shape for an unusable id: the
    frontmatter clause already refuses it, and answering 200 keeps the
    invariant that a content problem is never a request-shape error."""
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(id=""), "owner": "operator"})
    assert r.status_code == 200
    body = r.json()
    assert body["may_land"] is False
    assert body["clauses_refused"] == ["frontmatter"]


def test_an_explicit_null_owner_is_refused_the_same_as_omission(client):
    """An earlier version of this route treated `"owner" in payload` as key
    PRESENCE, so an explicit null entered the owner-given branch and
    `_claim_owner` fell back to the token identity — the clause would have run
    against the SERVICE rather than the person, the refuse-every-save failure
    the gate exists to prevent, reached through null instead of omission. Now
    that omission itself is refused, null and omission both take the same
    path: `payload.get("owner")` reads `None` either way."""
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(), "owner": None})
    body = r.json()
    assert body["may_land"] is False
    assert body["clauses_checked"] == ["frontmatter", "checkout"]
    assert body["clauses_refused"] == ["checkout"]


def test_a_non_string_id_is_a_validation_error(client):
    """Pre-existing gap found while adding the clause: the presence loop saw the
    key and the format check was guarded by `isinstance`, so `{"id": 123}`
    validated clean and was answered may_land true."""
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(id=123)})
    body = r.json()
    assert body["may_land"] is False
    assert any("id must be a string" in e for e in body["errors"])


def test_an_unusable_id_with_an_owner_is_a_refusal_not_a_400(client, held):
    """The invariant `test_non_dict_frontmatter_is_a_may_land_false_not_a_400`
    holds even on the new path: a frontmatter content problem is answered 200
    with may_land false, never a request-shape error. The clause is left unrun
    and `clauses_checked` says so rather than claiming a check that could not
    happen — safe only because a non-string id now fails validation, so the
    answer is already a refusal."""
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(id=123), "owner": "operator"})
    assert r.status_code == 200
    body = r.json()
    assert body["may_land"] is False
    assert body["clauses_checked"] == ["frontmatter"]
    assert body["clauses_refused"] == ["frontmatter"]


def test_the_frontmatter_clause_refuses_alone_while_the_checkout_clause_passes(client, held):
    """THE MIRROR ISOLATION CASE, which the first round was missing. The other
    tests show the checkout clause failing with frontmatter valid; this shows
    frontmatter failing while the checkout clause PASSES, so an active checkout
    clause cannot mask or taint the frontmatter clause's own refusal."""
    held("library/spec/example", "operator")
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(kind="not-a-kind"), "owner": "operator"})
    body = r.json()
    assert body["may_land"] is False
    assert body["clauses_checked"] == ["frontmatter", "checkout"]
    assert body["clauses_refused"] == ["frontmatter"]


def test_naming_an_owner_without_reference_read_is_forbidden(client, held, monkeypatch):
    """The disclosure gate. Evaluating the clause tells a caller whether an
    arbitrary id is checked out, which the dedicated status route denies by
    requiring `reference.read`. Withholding only the holder's NAME was not
    enough: "no live checkout is held on X" versus "checked out to another
    holder" still distinguishes held from unheld. So the whole clause is gated.

    Monkeypatched because no role today holds `reference.admit` without
    `reference.read` — the gate is unreachable in the current role table and is
    here for the split the code comment treats as possible."""
    held("library/spec/example", "operator")
    real = identity.authorized

    def only_admit(ident, cap):
        return False if cap == "reference.read" else real(ident, cap)

    monkeypatch.setattr(identity, "authorized", only_admit)
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(), "owner": "operator"})
    assert r.status_code == 403


def test_omitting_owner_needs_no_reference_read(client, monkeypatch):
    """The `reference.read` gate guards the checkout LOOKUP, not the refusal
    for an absent owner — refusing an omitted owner discloses nothing about
    any document, so it does not need the capability a real lookup does."""
    real = identity.authorized

    def only_admit(ident, cap):
        return False if cap == "reference.read" else real(ident, cap)

    monkeypatch.setattr(identity, "authorized", only_admit)
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm()})
    assert r.status_code == 200
    body = r.json()
    assert body["may_land"] is False
    assert body["clauses_checked"] == ["frontmatter", "checkout"]


# --- end to end: the bug this issue exists to fix -------------------------------


def test_an_orient_editor_save_for_an_adopters_own_project_is_admitted(
        client, held, monkeypatch, tmp_path, registry):
    """`issue_01M3NS89KVRXB368VA4TJ38MDG`: a project `kb_create` made on a
    fresh install, saved through Orient's in-window editor, must not be
    refused for want of a row in the static baseline."""
    registry({"filler": {}})
    (tmp_path / "projects" / "adopter-app").mkdir(parents=True)
    monkeypatch.setattr(reference_admission, "KNOWLEDGE_ROOT", str(tmp_path))
    held("adopter-app/reference/example", "operator")
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(id="adopter-app/reference/example",
                                             project="adopter-app"),
                          "owner": "operator"})
    assert r.status_code == 200
    body = r.json()
    assert body["may_land"] is True, body
