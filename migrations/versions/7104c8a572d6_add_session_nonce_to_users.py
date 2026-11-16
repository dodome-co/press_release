"""users に session_nonce を追加

Revision ID: 7104c8a572d6
Revises: 6a03227e50fc
Create Date: 2026-11-16 12:18:59.621118

"""
import secrets

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '7104c8a572d6'
down_revision = '6a03227e50fc'
branch_labels = None
depends_on = None


def upgrade():
    # 既存のユーザーにも、ひとりずつ別の値を入れてから「必須」にする
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('session_nonce', sa.String(length=32), nullable=True))

    users = sa.table('users', sa.column('id', sa.Integer), sa.column('session_nonce', sa.String))
    conn = op.get_bind()
    for (user_id,) in conn.execute(sa.select(users.c.id)).fetchall():
        conn.execute(users.update().where(users.c.id == user_id).values(session_nonce=secrets.token_hex(16)))

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.alter_column('session_nonce', existing_type=sa.String(length=32), nullable=False)


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('session_nonce')
