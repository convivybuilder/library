"""machine_certificates: the mutual-TLS registry a signature from the root
is not enough to pass (platform/decisions/0066)

Revision ID: f71949b78df4
Revises: d3f81a6c07be
Create Date: 2026-09-01

**A NEW TABLE THIS SERVICE OWNS OUTRIGHT**, the same reasoning
`c68fdb59edfc` gives for `tool_policy`: nothing on Orient's side ever held a
machine certificate, so there is no prior schema for a model here to drift
away from.

**One row per issued certificate, not one row per machine.** A machine that
loses its key and re-enrolls gets a new row with a new fingerprint rather
than an update to the old one — the decision record is explicit that the two
certificates must never coexist as both valid, and history of what a machine
held is worth keeping rather than overwriting. `(machine, fingerprint_sha256)`
is looked up together on every mutual-TLS request; `fingerprint_sha256` alone
is unique because a SHA-256 collision between two real certificates is not a
risk this service designs against.

**`custody` names how the issued key is held, `hardware` or `file`, and has
no reader in this migration's own stage.** It exists for the fleet-wide
hardware-custody gate platform/decisions/0066 requires, so a client on a
file-based key is never silently exempted once the plain port goes away.

**Stage D's check reads FOR `hardware`, never against `file`**, and whoever
writes it should keep that direction. A gate worded "block while any row
reads `file`" passes every row that is neither — a null, an empty string, a
value nobody anticipated — which are precisely the unclear rows a fleet-wide
bar exists to catch (security review, 2026-09-01). `NOT NULL` and the check
constraint below make those unrepresentable HERE, and the reading direction
is still what matters, because the constraint protects this table rather
than whatever the gate is eventually written against.

**It considers ACTIVE machines only.** A revoked or decommissioned row is
excluded, or a retired machine last recorded as `file` blocks retirement
forever.

**And the value is reasserted at every issuance and renewal, never copied
forward.** It is typed by a human on the attended path that signs the
certificate, and renewal is the only moment that could refresh it. Carried
forward instead, a machine reissued with a file-based key keeps whatever was
last typed, and the gate passes on a row that has been wrong for months.

`NOT NULL` with no default, so nothing issued in the meantime can be recorded
without an answer to the question stage D depends on.

**`status` gets the same CHECK constraint `custody` does, and for the same
reason** (security review, 2026-09-01): `MachineIdentityGate` already reads
FOR `active` rather than against `revoked`, so a typo or a future status
value refuses in the application. The constraint makes the schema a
backstop rather than leaving that direction enforced only in Python.
"""
from alembic import op
import sqlalchemy as sa

revision = "f71949b78df4"
down_revision = "d3f81a6c07be"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "machine_certificates",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("machine", sa.String(64), nullable=False),
        sa.Column("fingerprint_sha256", sa.String(64), nullable=False,
                   unique=True),
        sa.Column("not_after", sa.DateTime, nullable=False),
        sa.Column("status", sa.String(10), nullable=False,
                   server_default="active"),
        sa.CheckConstraint("status IN ('active', 'revoked')",
                           name="ck_machine_certificates_status"),
        sa.Column("revoked_at", sa.DateTime, nullable=True),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("custody", sa.String(10), nullable=False),
        sa.CheckConstraint("custody IN ('hardware', 'file')",
                           name="ck_machine_certificates_custody"),
    )
    op.create_index("ix_machine_certificates_lookup", "machine_certificates",
                     ["machine", "fingerprint_sha256"])


def downgrade() -> None:
    op.drop_index("ix_machine_certificates_lookup",
                   table_name="machine_certificates")
    op.drop_table("machine_certificates")
