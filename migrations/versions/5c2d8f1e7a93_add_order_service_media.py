"""注文に、動画を出す媒体の列を追加

Revision ID: 5c2d8f1e7a93
Revises: 3b7e1c9a4d20
Create Date: 2026-11-17 19:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '5c2d8f1e7a93'
down_revision = '3b7e1c9a4d20'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.add_column(sa.Column('service_media', sa.String(length=30), nullable=True))


def downgrade():
    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.drop_column('service_media')
