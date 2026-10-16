"""machine_certificates.custody accepts root-file: a software key only root
can read (platform/decisions/0092)

Revision ID: b7e2d4c91a35
Revises: a6d1c1b1974e
Create Date: 2026-10-15

**One CHECK constraint widens, nothing else.** `ck_machine_certificates_custody`
goes from `hardware, file` to `hardware, file, root-file`. No row changes and
no column changes. SQLite cannot alter a CHECK in place, so batch mode rebuilds
the table: it copies every row into a new table with the new constraint, drops
the old one and renames, inside the migration's transaction. The table holds a
handful of rows.

**Why a third value rather than `file`.** `file` means a key the operator
account can read, so any agent running as that account can read it, which is
why the Bosun refuses `file` for session registration. The Individuator's
daemon key is a software key in a file only root can read, because a
launchd daemon cannot use the Secure Enclave on macOS (the operator,
2026-10-15). Recorded as `file`, the two would be indistinguishable to every
client that reads custody.

**The downgrade narrows the CHECK back**, and refuses rather than rewrites if
any row reads `root-file`: relabelling a root-held key as `file` or `hardware`
would be a false attestation, so a downgrade with such rows waits for a person.

**The table is described here rather than reflected** (`copy_from`), because
the offline preview (`alembic upgrade head --sql`) has no database to reflect.
The description is the live schema exactly: every column, both CHECKs, the
unique fingerprint and the lookup index, so the rebuild drops nothing.
"""
from alembic import op
import sqlalchemy as sa


revision = 'b7e2d4c91a35'
down_revision = 'a6d1c1b1974e'
branch_labels = None
depends_on = None

_NAME = "ck_machine_certificates_custody"
_WIDE = "custody IN ('hardware', 'file', 'root-file')"
_NARROW = "custody IN ('hardware', 'file')"


def _table(custody_check: str) -> sa.Table:
    return sa.Table(
        "machine_certificates", sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("machine", sa.String(64), nullable=False),
        sa.Column("fingerprint_sha256", sa.String(64), nullable=False,
                  unique=True),
        sa.Column("not_after", sa.DateTime, nullable=False),
        sa.Column("status", sa.String(10), nullable=False,
                  server_default="active"),
        sa.Column("revoked_at", sa.DateTime, nullable=True),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("custody", sa.String(10), nullable=False),
        sa.CheckConstraint("status IN ('active', 'revoked')",
                           name="ck_machine_certificates_status"),
        sa.CheckConstraint(custody_check, name=_NAME),
        sa.Index("ix_machine_certificates_lookup", "machine",
                 "fingerprint_sha256"),
    )


def upgrade() -> None:
    with op.batch_alter_table("machine_certificates", recreate="always",
                              copy_from=_table(_NARROW)) as batch:
        batch.drop_constraint(_NAME, type_="check")
        batch.create_check_constraint(_NAME, _WIDE)


def downgrade() -> None:
    bind = op.get_bind()
    count = bind.execute(sa.text(
        "SELECT count(*) FROM machine_certificates WHERE custody = 'root-file'"
    )).scalar()
    if count:
        raise RuntimeError(
            f"{count} machine_certificates row(s) read custody 'root-file'; "
            f"narrowing the constraint would need them relabelled, which "
            f"would misstate how those keys are held. Revoke and re-enroll "
            f"them first.")
    with op.batch_alter_table("machine_certificates", recreate="always",
                              copy_from=_table(_WIDE)) as batch:
        batch.drop_constraint(_NAME, type_="check")
        batch.create_check_constraint(_NAME, _NARROW)
