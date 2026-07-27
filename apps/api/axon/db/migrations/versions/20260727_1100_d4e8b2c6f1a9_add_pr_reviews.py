"""Add pr_reviews table (AI review of a pull request)

One row per reviewed PR revision: unique on (repo_id, pr_number, head_sha) so
re-reviewing an unchanged PR is a no-op while a new push yields a fresh review.

JobKind gains "review_pr" but needs no DDL — the enum columns are plain
VARCHAR(32) (native_enum=False, create_constraint defaults off).

Revision ID: d4e8b2c6f1a9
Revises: c7f3a9b1d2e4
Create Date: 2026-07-27 11:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "d4e8b2c6f1a9"
down_revision = "c7f3a9b1d2e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pr_reviews",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("repo_id", sa.Uuid(), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("head_sha", sa.String(length=64), nullable=False),
        sa.Column("pr_title", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "comments",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("review_url", sa.String(length=512), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(["repo_id"], ["repos.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "repo_id", "pr_number", "head_sha", name="uq_pr_reviews_repo_pr_sha"
        ),
    )
    op.create_index("ix_pr_reviews_repo_pr", "pr_reviews", ["repo_id", "pr_number"])


def downgrade() -> None:
    op.drop_index("ix_pr_reviews_repo_pr", table_name="pr_reviews")
    op.drop_table("pr_reviews")
