"""Replace Monzo disconnection flags with one connection status."""

import sqlalchemy as sa
from alembic import op

revision = "0017_connection_status"
down_revision = "0016_refresh_inactivity_expiry"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "monzo_credentials",
        sa.Column(
            "connection_status",
            sa.String(length=32),
            nullable=False,
            server_default="connected",
        ),
    )
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE monzo_credentials "
            "SET connection_status = CASE "
            "WHEN revocation_pending THEN 'revocation_pending' "
            "WHEN disconnected THEN 'disconnected' "
            "ELSE 'connected' END"
        )
    )
    with op.batch_alter_table("monzo_credentials") as batch:
        batch.drop_column("revocation_pending")
        batch.drop_column("disconnected")
        batch.create_check_constraint(
            "ck_monzo_credentials_connection_status",
            "connection_status IN ('connected', 'revocation_pending', 'disconnected')",
        )


def downgrade():
    op.add_column(
        "monzo_credentials",
        sa.Column(
            "disconnected", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "monzo_credentials",
        sa.Column(
            "revocation_pending",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE monzo_credentials "
            "SET disconnected = connection_status IN ('disconnected', 'revocation_pending'), "
            "revocation_pending = connection_status = 'revocation_pending'"
        )
    )
    with op.batch_alter_table("monzo_credentials") as batch:
        batch.drop_constraint(
            "ck_monzo_credentials_connection_status", type_="check"
        )
        batch.drop_column("connection_status")
