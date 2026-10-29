"""publishing accounts and jobs (D14: YouTube/TikTok publishing)

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-29 08:33:29.056378
"""
from alembic import op
import sqlalchemy as sa


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('publish_accounts',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('platform', sa.String(length=16), nullable=False),
    sa.Column('external_account_id', sa.String(length=200), nullable=False),
    sa.Column('display_name', sa.String(length=200), nullable=False),
    sa.Column('thumbnail_url', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('credential_key', sa.String(length=80), nullable=False),
    sa.Column('scopes', sa.JSON(), nullable=False),
    sa.Column('auto_publish', sa.Boolean(), nullable=False),
    sa.Column('connected_at', sa.DateTime(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('credential_key'),
    sa.UniqueConstraint('platform', 'external_account_id', name='uq_account_identity')
    )
    op.create_table('publish_jobs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('render_id', sa.String(length=36), nullable=False),
    sa.Column('account_id', sa.String(length=36), nullable=True),
    sa.Column('platform', sa.String(length=16), nullable=False),
    sa.Column('account_label', sa.String(length=200), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('tags', sa.JSON(), nullable=False),
    sa.Column('privacy', sa.String(length=16), nullable=False),
    sa.Column('scheduled_for', sa.DateTime(), nullable=True),
    sa.Column('scheduled_tz', sa.String(length=64), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('platform_upload_id', sa.String(length=200), nullable=True),
    sa.Column('platform_post_id', sa.String(length=200), nullable=True),
    sa.Column('platform_post_url', sa.Text(), nullable=True),
    sa.Column('last_operation', sa.String(length=200), nullable=False),
    sa.Column('error_code', sa.String(length=48), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('retry_count', sa.Integer(), nullable=False),
    sa.Column('job_id', sa.String(length=36), nullable=True),
    sa.Column('published_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['publish_accounts.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['render_id'], ['rendered_clips.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('publish_jobs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_publish_jobs_account_id'), ['account_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_publish_jobs_job_id'), ['job_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_publish_jobs_render_id'), ['render_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_publish_jobs_status'), ['status'], unique=False)
        batch_op.create_index('uq_publish_active_per_account', ['render_id', 'account_id'], unique=True, sqlite_where=sa.text("status IN ('queued','uploading')"))

    # ### end Alembic commands ###


def downgrade() -> None:
    with op.batch_alter_table('publish_jobs', schema=None) as batch_op:
        batch_op.drop_index('uq_publish_active_per_account', sqlite_where=sa.text("status IN ('queued','uploading')"))
        batch_op.drop_index(batch_op.f('ix_publish_jobs_status'))
        batch_op.drop_index(batch_op.f('ix_publish_jobs_render_id'))
        batch_op.drop_index(batch_op.f('ix_publish_jobs_job_id'))
        batch_op.drop_index(batch_op.f('ix_publish_jobs_account_id'))

    op.drop_table('publish_jobs')
    op.drop_table('publish_accounts')
    # ### end Alembic commands ###
