"""signing_certificates: the registry a signed knowledge save's certificate
answers to (platform/contracts/library-protocol §2.2)

Revision ID: 7a43066dc44d
Revises: a8d2c5e91f37
Create Date: 2026-09-27 13:34:44.055110

**Beside `machine_certificates`, and apart from it** — the contract's own
words for the shape. A signing certificate authenticates no connection, and
a machine certificate carries no signing purpose (§2.2: "the two URI schemes
are disjoint on purpose"), so the two registries share no row and this table
has no foreign key to the other.

**One row per certificate, and MULTIPLE ACTIVE ROWS PER PERSON ARE
EXPECTED, not an anomaly `issue()` cleans up.** This is the opposite of
`machine_certificates`' own invariant: that table revokes whatever was
active for a machine in the same transaction as a new row, because ADR-0066
holds two live certificates for one machine name as the attacker's own
story ("I lost my key"). A person is not a machine name. §2.2 is explicit
that a person may sign from more than one Mac at a time ("naming the Mac
that holds the key" is per-certificate, not per-person), and a Touch-ID
ceremony that revoked every other Mac's signing key on every new enrollment
would make owning a second Mac cost the first one's signing ability. So
`issue()` here is a plain insert, never an update, and there is no
`(person)` uniqueness for it to enforce.

**Revoke targets a fingerprint, not a person or a machine.** Given several
active rows for the same person, "revoke this person" is ambiguous about
which Mac's key it means; the fingerprint is the one value that names
exactly one row, and it is what `enroll_machine.py`'s sibling command
already has in hand, printed by the same signing ceremony that mints the
certificate.

**`fingerprint_sha256` is UNIQUE, alone, matching `machine_certificates`.**
A SHA-256 collision between two real certificates is not a risk this
service designs against, and the registry route (`GET /signing-certificates/
{fingerprint}`) looks up by nothing else — there is no compound lookup here
the way `(machine, fingerprint_sha256)` is for the mutual-TLS listener's
per-request read, so no extra index is added beside the one the UNIQUE
constraint already gives.

**`status` carries the same CHECK constraint `machine_certificates.status`
does, and for the same reason** (security review, 2026-09-01, carried
forward): the registry route reads FOR `active` rather than against
`revoked`, so a typo or a future status value refuses at the read rather
than passing silently. `expired` is not a stored status — it is computed at
read time from `not_after`, the same way the contract states it ("expired
means the registry row's not_after has passed"), so the CHECK only ever has
to rule out something other than the two states a row can actually hold.

**`issued_at`, not `created_at`.** The neighbouring table calls its
timestamp `created_at`; this one calls it `issued_at` to match `issue()`,
the one function that ever writes it, and the vocabulary §2.2 itself uses
for the act ("the root-ca tool issues it in the Touch-ID-gated ceremony").
"""
from alembic import op
import sqlalchemy as sa


revision = '7a43066dc44d'
down_revision = 'a8d2c5e91f37'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "signing_certificates",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("fingerprint_sha256", sa.String(64), nullable=False,
                   unique=True),
        sa.Column("person", sa.String(200), nullable=False),
        sa.Column("machine", sa.String(64), nullable=False),
        sa.Column("not_after", sa.DateTime, nullable=False),
        sa.Column("status", sa.String(10), nullable=False,
                   server_default="active"),
        sa.CheckConstraint("status IN ('active', 'revoked')",
                           name="ck_signing_certificates_status"),
        sa.Column("issued_at", sa.DateTime, nullable=False),
        sa.Column("revoked_at", sa.DateTime, nullable=True),
        sa.Column("note", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("signing_certificates")
