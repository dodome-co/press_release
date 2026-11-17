"""注文に支払い画面の履歴を追加し、金額の下限を50円にする

Revision ID: 8a9d2e60d520
Revises: 866bed17fc8b
Create Date: 2026-11-17 09:57:23.052167

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '8a9d2e60d520'
down_revision = '866bed17fc8b'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('order_checkout_sessions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('order_id', sa.Integer(), nullable=False),
    sa.Column('stripe_session_id', sa.String(length=255), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('stripe_session_id')
    )
    with op.batch_alter_table('order_checkout_sessions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_order_checkout_sessions_order_id'), ['order_id'], unique=False)

    # 既存の注文は、支払い画面を1回作ったものとして扱う（既存の行にも値が入るよう、既定値 0 を付ける）
    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.add_column(sa.Column('checkout_attempts', sa.Integer(), nullable=False, server_default='0'))
    op.execute(
        "INSERT INTO order_checkout_sessions (order_id, stripe_session_id, created_at) "
        "SELECT id, stripe_session_id, created_at FROM orders WHERE stripe_session_id IS NOT NULL"
    )
    op.execute("UPDATE orders SET checkout_attempts = 1 WHERE stripe_session_id IS NOT NULL")

    # 金額の下限：0円以上 → 50円以上（Stripe は日本円で50円未満を決済できない）
    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.drop_constraint('ck_orders_amount_nonnegative', type_='check')
        batch_op.create_check_constraint('ck_orders_amount_min', 'amount_yen >= 50')


def downgrade():
    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.drop_constraint('ck_orders_amount_min', type_='check')
        batch_op.create_check_constraint('ck_orders_amount_nonnegative', 'amount_yen >= 0')
        batch_op.drop_column('checkout_attempts')

    with op.batch_alter_table('order_checkout_sessions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_order_checkout_sessions_order_id'))

    op.drop_table('order_checkout_sessions')
