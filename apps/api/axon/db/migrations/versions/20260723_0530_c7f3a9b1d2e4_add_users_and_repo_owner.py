"""Add users table and repos.owner_id

Identity + per-user ownership: repos are now scoped to an authenticated user
(GitHub OAuth). owner_id is nullable so repos connected before auth keep
working and are claimed on reconnect; SET NULL on user delete preserves the
repo and its findings history.

Revision ID: c7f3a9b1d2e4
Revises: 2b883e7a16d0
Create Date: 2026-07-23 05:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c7f3a9b1d2e4"
down_revision = "2b883e7a16d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("github_id", sa.BigInteger(), nullable=False),
        sa.Column("login", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("avatar_url", sa.String(length=512), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("access_token", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("github_id", name="uq_users_github_id"),
    )
    op.add_column("repos", sa.Column("owner_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_repos_owner_id_users",
        "repos",
        "users",
        ["owner_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_repos_owner", "repos", ["owner_id"])


def downgrade() -> None:
    op.drop_index("ix_repos_owner", table_name="repos")
    op.drop_constraint("fk_repos_owner_id_users", "repos", type_="foreignkey")
    op.drop_column("repos", "owner_id")
    op.drop_table("users")
