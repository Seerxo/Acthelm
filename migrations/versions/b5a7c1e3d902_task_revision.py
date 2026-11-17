"""Per-task revision counter for the B5 Desk -> Stage C handoff reports.

Revision ID: b5a7c1e3d902
Revises: f6c8a2d9e410
"""
import sqlalchemy as sa
from alembic import op

revision = "b5a7c1e3d902"
down_revision = "f6c8a2d9e410"
branch_labels = None
depends_on = None


def upgrade():
    # A constant server default is metadata-only on PostgreSQL 11+: no table rewrite. Still give up instead of
    # queueing behind a long transaction for the brief ACCESS EXCLUSIVE lock.
    if op.get_context().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("tasks", sa.Column("revision", sa.Integer(), server_default="0", nullable=False))


def downgrade():
    op.drop_column("tasks", "revision")
