"""add source_media.source_url (import-by-URL provenance)

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-29 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('source_media', schema=None) as batch_op:
        batch_op.add_column(sa.Column('source_url', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('source_media', schema=None) as batch_op:
        batch_op.drop_column('source_url')
