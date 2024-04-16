"""Update size of build images. [743ec82b1b3c].

Revision ID: 743ec82b1b3c
Revises: 9971237fa937
Create Date: 2023-05-09 10:08:56.544542

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = "743ec82b1b3c"
down_revision = "9971237fa937"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Upgrade database schema and/or data, creating a new revision."""
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table("pipeline_build", schema=None) as batch_op:
        batch_op.alter_column(
            "images",
            existing_type=sa.TEXT(),
            type_=sa.String(length=16777215).with_variant(
                mysql.MEDIUMTEXT(), "mysql"
            ),
            existing_nullable=False,
        )

    # ### end Alembic commands ###


def downgrade() -> None:
    """Downgrade database schema and/or data back to the previous revision."""
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table("pipeline_build", schema=None) as batch_op:
        batch_op.alter_column(
            "images",
            existing_type=sa.String(length=16777215).with_variant(
                mysql.MEDIUMTEXT(), "mysql"
            ),
            type_=sa.TEXT(),
            existing_nullable=False,
        )

    # ### end Alembic commands ###