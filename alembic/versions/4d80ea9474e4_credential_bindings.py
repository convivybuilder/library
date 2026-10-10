"""credential_bindings: the mapping stage C checks a bearer credential
against (platform/decisions/0066)

Revision ID: 4d80ea9474e4
Revises: f2f766662793
Create Date: 2026-09-04

**The SHA-256 of the bearer credential, never the credential itself.** The
raw token authenticates every other route in this service; nothing about
this table's purpose changes that, and a column that could hold it is a
column that eventually will. `credential_sha256` is a hex digest, indexable
and comparable like the machine registry's own `fingerprint_sha256`, over
a value that reveals nothing about the token it was computed from.

**One row per credential, not one row per binding event.** A credential
binds to exactly one machine at a time — `credential_sha256` is `UNIQUE`,
so rebinding a credential (the lockout recovery path
`platform/decisions/0066` specifies) is an upsert onto the same row rather
than a second row shadowing the first. This is a real difference from
`machine_certificates`, where a re-enrolled machine intentionally gets a
new row so the old certificate's history survives — a credential has no
equivalent history worth keeping, since the token itself never rotates
here and only the machine it may be presented from does.

**No `status` column.** `machine_certificates` needs `active`/`revoked`
because a certificate keeps existing after it stops being trusted, and
"trusted" has to be a value the row can carry. A binding has no such
state: the row's presence for a given hash *is* "bound", and removing it
is what makes a credential unbound again — there is nothing a `status`
column would let this schema say that deleting the row does not already
say more simply.

**No `custody` column.** That column exists on `machine_certificates` for
stage D's fleet-wide hardware-custody gate, which is a property of a
machine's key, not of a bearer credential. This table has no stage-D role.

**Provenance is `created_at` and an optional `note`** — the same shape
`machine_certificates` uses for the fields that are not part of the lookup
itself, sized to what an administrative command run by a human typing at a
terminal can usefully record.

**`machine` carries no foreign key to `machine_certificates`.** The two
registries are checked by different gates at different points in the
request, `machine_identity.MachineIdentityGate` against the certificate and
this table's reader against the credential, and stage C's own text says the
binding is superseded at Phase 5 by a column on the library-card row —
tying this table's lifetime to `machine_certificates`' schema with a
constraint would outlive the reason to.
"""
from alembic import op
import sqlalchemy as sa

revision = "4d80ea9474e4"
down_revision = "f2f766662793"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "credential_bindings",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("credential_sha256", sa.String(64), nullable=False,
                   unique=True),
        sa.Column("machine", sa.String(64), nullable=False),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("credential_bindings")
