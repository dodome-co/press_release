"""取材会の日時と申込の締切を、文字列から日付型・時刻型に変更

Revision ID: 3b7e1c9a4d20
Revises: 15521234947d
Create Date: 2026-11-17 18:00:00.000000

注意：これまで入力された日時の文字列は、日付型に変換できないため残らない（デモ用データのみの想定）。
"""
from alembic import op
import sqlalchemy as sa


revision = '3b7e1c9a4d20'
down_revision = '15521234947d'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('releases', schema=None) as batch_op:
        batch_op.add_column(sa.Column('event_date', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('event_start_time', sa.Time(), nullable=True))
        batch_op.add_column(sa.Column('event_end_time', sa.Time(), nullable=True))
        batch_op.add_column(sa.Column('application_deadline_at', sa.DateTime(), nullable=True))
        batch_op.drop_column('event_at_text')
        batch_op.drop_column('application_deadline')


def downgrade():
    with op.batch_alter_table('releases', schema=None) as batch_op:
        batch_op.add_column(sa.Column('application_deadline', sa.String(length=100), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('event_at_text', sa.String(length=200), nullable=False, server_default=''))
        batch_op.drop_column('application_deadline_at')
        batch_op.drop_column('event_end_time')
        batch_op.drop_column('event_start_time')
        batch_op.drop_column('event_date')
