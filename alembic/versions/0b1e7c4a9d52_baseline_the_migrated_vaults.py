"""baseline: the vaults as migrated out of Orient's control database

Revision ID: 0b1e7c4a9d52
Revises:
Create Date: 2026-08-16

**THIS REVISION IS STAMPED, NOT RUN, ON THE DATABASE THE CUTOVER PRODUCES** (the
cutover sequence, Stage B step 5). `migrate.py` writes that file by copying
Orient's schema and its rows, so every table already exists when alembic first
meets it. `alembic stamp head` records that this revision's state is present and
`upgrade head` is a no-op from day one, which is what gives every later Library
schema change an ancestor to attach to.

**It is authored to run anyway, because a baseline that cannot build a database
is a baseline nobody can check anything against.** A fresh Library — a test, a
second deployment, a restore — gets its schema from here, and an `upgrade` that
was a `pass` would describe a schema no file could be compared to.

**The DDL is the statement SQLite reports for each table, not an `op.create_table`
transcription of it.** `store` holds raw SQL with explicit column lists rather
than ORM models, deliberately: during the shadow the rows belong to Orient, and a
model here would be a second definition of a schema free to drift with nothing to
catch it. Twelve hand-written `sa.Column` tables would reintroduce that at the one
place being wrong is silent — the copy is verified against Orient's live schema
column by column, where a transcription would only ever be checked against itself.

**The two vec0 tables are absent, and their absence is the point.** A vec0 virtual
table is invisible to a connection that has not loaded `sqlite-vec`, and alembic's
connection does not load it; that seam killed a migration once already
(`platform/reference/keel-goal-baselines`). They reach the Library through
`migrate.py`, which moves them across the vec0 interface. A database built from
this baseline alone therefore has no vector index — see the module docstring in
`alembic/env.py` for what that costs and does not cost.
"""
from alembic import op

revision = "0b1e7c4a9d52"
down_revision = None
branch_labels = None
depends_on = None

# The twelve moving tables in dependency order, each followed by its indexes.
# Generated on 2026-08-16 from the file `migrate.py` produced against a
# `VACUUM INTO` snapshot of the live 240 MB control database, then read back off
# `sqlite_master` rather than typed.
STATEMENTS = (
    # issue_items
    """\
CREATE TABLE "issue_items" (
    id VARCHAR(40) NOT NULL,
    product VARCHAR(64) NOT NULL,
    title VARCHAR(300) NOT NULL,
    body TEXT NOT NULL,
    refs JSON NOT NULL,
    severity VARCHAR(20) NOT NULL,
    state VARCHAR(20) NOT NULL,
    claimed_by VARCHAR(200),
    claimed_at DATETIME,
    resolution VARCHAR(20),
    resolved_by VARCHAR(200),
    resolved_at DATETIME,
    outcome_note TEXT,
    once_key VARCHAR(300),
    created_by VARCHAR(200) NOT NULL,
    created_at DATETIME NOT NULL,
    run_id VARCHAR(100),
    type VARCHAR(20) DEFAULT 'bug' NOT NULL,
    created_by_kind VARCHAR(10),
    needs VARCHAR(20),
    park_reason TEXT,
    unpark_condition TEXT,
    parked_at DATETIME,
    embedded_at DATETIME,
    parent_id VARCHAR(40),
    topics JSON, maturity VARCHAR(16), specification TEXT, definition_of_done TEXT,
    PRIMARY KEY (id),
    CONSTRAINT fk_issue_items_parent_id FOREIGN KEY(parent_id) REFERENCES issue_items (id) ON DELETE RESTRICT,
    UNIQUE (once_key)
)
    """,
    """CREATE INDEX ix_issue_items_product ON issue_items (product)""",
    """CREATE INDEX ix_issue_items_state ON issue_items (state)""",
    """CREATE INDEX ix_issue_items_created_at ON issue_items (created_at)""",
    """CREATE INDEX ix_issue_items_embedded_at ON issue_items (embedded_at)""",
    """CREATE INDEX ix_issue_items_severity ON issue_items (severity)""",
    """CREATE INDEX ix_issue_items_needs ON issue_items (needs)""",
    """CREATE INDEX ix_issue_items_type ON issue_items (type)""",
    """CREATE INDEX ix_issue_items_parent_id ON issue_items (parent_id)""",
    """CREATE INDEX ix_issue_items_maturity ON issue_items (maturity)""",
    # issue_comments
    """\
CREATE TABLE issue_comments (
    id INTEGER NOT NULL,
    item_id VARCHAR NOT NULL,
    actor VARCHAR(200) NOT NULL,
    kind VARCHAR(20) DEFAULT 'note' NOT NULL,
    body TEXT NOT NULL,
    run_id VARCHAR(100),
    at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(item_id) REFERENCES issue_items (id) ON DELETE CASCADE
)
    """,
    """CREATE INDEX ix_issue_comments_item_id ON issue_comments (item_id)""",
    # issue_events
    """\
CREATE TABLE "issue_events" (
    id INTEGER NOT NULL,
    item_id VARCHAR(40) NOT NULL,
    actor VARCHAR(200) NOT NULL,
    from_state VARCHAR(20),
    to_state VARCHAR(20) NOT NULL,
    note TEXT,
    at DATETIME NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(item_id) REFERENCES "issue_items" (id) ON DELETE CASCADE
)
    """,
    """CREATE INDEX ix_issue_events_item_id ON issue_events (item_id)""",
    # issue_leases
    """\
CREATE TABLE issue_leases (
    id VARCHAR(40) NOT NULL,
    root_id VARCHAR(40) NOT NULL,
    taken_for VARCHAR(40) NOT NULL,
    holder VARCHAR(200) NOT NULL,
    run_id VARCHAR(100),
    taken_at DATETIME NOT NULL,
    heartbeat_at DATETIME NOT NULL,
    released_at DATETIME,
    release_reason VARCHAR(20),
    released_by VARCHAR(200),
    note TEXT,
    PRIMARY KEY (id),
    CONSTRAINT fk_issue_leases_root_id FOREIGN KEY(root_id) REFERENCES issue_items (id) ON DELETE RESTRICT
)
    """,
    """CREATE INDEX ix_issue_leases_root_id ON issue_leases (root_id)""",
    """CREATE INDEX ix_issue_leases_taken_for ON issue_leases (taken_for)""",
    """CREATE INDEX ix_issue_leases_holder ON issue_leases (holder)""",
    """CREATE INDEX ix_issue_leases_release_reason ON issue_leases (release_reason)""",
    """CREATE UNIQUE INDEX ix_issue_leases_live_root ON issue_leases (root_id) WHERE released_at IS NULL""",
    # lease_refusals
    """\
CREATE TABLE lease_refusals (
    id VARCHAR(40) NOT NULL,
    at DATETIME NOT NULL,
    requested_item_id VARCHAR(40) NOT NULL,
    product VARCHAR(64) NOT NULL,
    requester VARCHAR(200) NOT NULL,
    actor VARCHAR(200) NOT NULL,
    blocking_lease_id VARCHAR(40) NOT NULL,
    blocking_root_id VARCHAR(40) NOT NULL,
    blocking_holder VARCHAR(200) NOT NULL,
    blocks_by VARCHAR(20) NOT NULL,
    PRIMARY KEY (id)
)
    """,
    """CREATE INDEX ix_lease_refusals_at ON lease_refusals (at)""",
    """CREATE INDEX ix_lease_refusals_actor ON lease_refusals (actor)""",
    """CREATE INDEX ix_lease_refusals_requested_item_id ON lease_refusals (requested_item_id)""",
    """CREATE INDEX ix_lease_refusals_blocking_root_id ON lease_refusals (blocking_root_id)""",
    """CREATE INDEX ix_lease_refusals_product_at ON lease_refusals (product, at)""",
    # courses
    """\
CREATE TABLE courses (
    id VARCHAR(40) NOT NULL,
    product VARCHAR(64) NOT NULL,
    title VARCHAR(300) NOT NULL,
    entries JSON NOT NULL,
    status VARCHAR(16) NOT NULL,
    version INTEGER NOT NULL,
    created_by VARCHAR(200) NOT NULL,
    created_at DATETIME NOT NULL,
    updated_by VARCHAR(200),
    updated_at DATETIME,
    PRIMARY KEY (id)
)
    """,
    """CREATE INDEX ix_courses_product ON courses (product)""",
    """CREATE INDEX ix_courses_status ON courses (status)""",
    """CREATE INDEX ix_courses_created_at ON courses (created_at)""",
    """CREATE UNIQUE INDEX ix_courses_live_product ON courses (product) WHERE status = 'live'""",
    # course_events
    """\
CREATE TABLE course_events (
    id INTEGER NOT NULL,
    course_id VARCHAR(40) NOT NULL,
    actor VARCHAR(200) NOT NULL,
    action VARCHAR(20) NOT NULL,
    entries JSON,
    note TEXT,
    at DATETIME NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(course_id) REFERENCES courses (id) ON DELETE CASCADE
)
    """,
    """CREATE INDEX ix_course_events_course_id ON course_events (course_id)""",
    # signals
    """\
CREATE TABLE "signals" (
    id VARCHAR(40) NOT NULL,
    source VARCHAR(40) NOT NULL,
    stream VARCHAR(200),
    kind VARCHAR(40) NOT NULL,
    actor VARCHAR(200) NOT NULL,
    ingested_by VARCHAR(200) NOT NULL,
    product VARCHAR(64) NOT NULL,
    refs JSON NOT NULL,
    body TEXT NOT NULL,
    meta JSON NOT NULL,
    privacy_class VARCHAR(20) NOT NULL,
    produced_at DATETIME,
    ingested_at DATETIME NOT NULL,
    embedded_at DATETIME,
    dedup_key VARCHAR(300),
    PRIMARY KEY (id),
    CONSTRAINT uq_signals_dedup_key UNIQUE (dedup_key)
)
    """,
    """CREATE INDEX ix_signals_ingested_at ON signals (ingested_at)""",
    """CREATE INDEX ix_signals_stream ON signals (stream)""",
    """CREATE INDEX ix_signals_embedded_at ON signals (embedded_at)""",
    """CREATE INDEX ix_signals_privacy_class ON signals (privacy_class)""",
    """CREATE INDEX ix_signals_source ON signals (source)""",
    """CREATE INDEX ix_signals_product ON signals (product)""",
    # signal_acks
    """\
CREATE TABLE signal_acks (
    id VARCHAR(40) NOT NULL,
    signal_id VARCHAR(40) NOT NULL,
    identity VARCHAR(200) NOT NULL,
    acked_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_signal_acks_signal_identity UNIQUE (signal_id, identity)
)
    """,
    """CREATE INDEX ix_signal_acks_signal_id ON signal_acks (signal_id)""",
    """CREATE INDEX ix_signal_acks_identity ON signal_acks (identity)""",
    # decision_items
    """\
CREATE TABLE "decision_items" (
    id VARCHAR(40) NOT NULL,
    product VARCHAR(64) NOT NULL,
    kind VARCHAR(40) NOT NULL,
    title VARCHAR(300) NOT NULL,
    body TEXT NOT NULL,
    refs JSON NOT NULL,
    state VARCHAR(20) NOT NULL,
    decided_by VARCHAR(200),
    decided_at DATETIME,
    directive TEXT,
    snooze_until DATETIME,
    once_key VARCHAR(200),
    created_by VARCHAR(200) NOT NULL,
    created_at DATETIME NOT NULL,
    run_id VARCHAR(100),
    outcome_note TEXT,
    blocked BOOLEAN DEFAULT 0 NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (once_key)
)
    """,
    """CREATE INDEX ix_decision_items_state ON decision_items (state)""",
    """CREATE INDEX ix_decision_items_created_at ON decision_items (created_at)""",
    """CREATE INDEX ix_decision_items_product ON decision_items (product)""",
    """CREATE INDEX ix_decision_items_blocked ON decision_items (blocked)""",
    # decision_comments
    """\
CREATE TABLE decision_comments (
    id INTEGER NOT NULL,
    item_id VARCHAR(40) NOT NULL,
    actor VARCHAR(200) NOT NULL,
    body TEXT NOT NULL,
    at DATETIME NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(item_id) REFERENCES decision_items (id) ON DELETE CASCADE
)
    """,
    """CREATE INDEX ix_decision_comments_item_id ON decision_comments (item_id)""",
    # decision_events
    """\
CREATE TABLE decision_events (
    id INTEGER NOT NULL,
    item_id VARCHAR(40) NOT NULL,
    actor VARCHAR(200) NOT NULL,
    from_state VARCHAR(20),
    to_state VARCHAR(20) NOT NULL,
    directive TEXT,
    at DATETIME NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(item_id) REFERENCES decision_items (id) ON DELETE CASCADE
)
    """,
    """CREATE INDEX ix_decision_events_item_id ON decision_events (item_id)""",)


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    """**There is no downgrade from the baseline, and a working one would be the
    hazard.** Reversing this drops every vault table — 797 issues, 19,047 signals
    and both vector indexes when it was written — and alembic puts no confirmation
    between typing `downgrade base` and losing them. The supported way back is the
    backup Stage B step 2 takes, which is also the one the cutover's rollback
    names.
    """
    raise NotImplementedError(
        "the baseline has no downgrade: it would drop every vault table. "
        "Restore the backup taken at the cutover instead.")
