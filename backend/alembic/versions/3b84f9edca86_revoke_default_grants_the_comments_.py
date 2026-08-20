"""revoke default grants the comments already claimed

Revision ID: 3b84f9edca86
Revises: 2e3ebe7f926a
Create Date: 2026-08-20 17:41:33.000000

Two earlier migrations documented restrictions they did not actually apply.

`init.sh` sets ALTER DEFAULT PRIVILEGES granting `arwd` to app_rw and SELECT to
app_readonly on every table created afterwards. A migration that then GRANTs a
narrower set does not remove what the default already gave — so:

  - fc6dfa15d184 said app_readonly was deliberately NOT granted on `connector`,
    "a credential column is not something to hand out by default". It had
    SELECT the whole time.
  - 2e3ebe7f926a said there was no DELETE on `approval_request` so the
    application "cannot erase the trail". app_rw had DELETE.

Neither was exploited — nothing in the code reads connector credentials as
app_readonly, and nothing deletes approval rows. But a comment asserting a
control that is not there is worse than no comment: it is what someone relies on
when deciding whether a change is safe.

This applies the restrictions the comments describe. Found by writing a test
that tried the forbidden operation instead of trusting the GRANT statement.
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3b84f9edca86"
down_revision: Union[str, Sequence[str], None] = "2e3ebe7f926a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # An approval record is the evidence that a named human authorised a write
    # which reached a production system. The application proposes, decides, and
    # executes; it must not be able to erase the record of having done so.
    # Retention deletes these, and retention runs as the owner.
    op.execute("REVOKE DELETE ON approval_request FROM app_rw")

    # `connector` carries an encrypted credential column. app_readonly backs
    # reporting paths; nothing there needs connector configuration, and a
    # credential column is not something to hand out by default — even
    # encrypted, even to a read-only role.
    op.execute("REVOKE SELECT ON connector FROM app_readonly")
    op.execute("REVOKE SELECT ON approval_request FROM app_readonly")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("GRANT DELETE ON approval_request TO app_rw")
    op.execute("GRANT SELECT ON connector TO app_readonly")
    op.execute("GRANT SELECT ON approval_request TO app_readonly")
