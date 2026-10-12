
import os

from dotenv import load_dotenv
from sqlalchemy import inspect, text
from sqlmodel import SQLModel, create_engine, Session

load_dotenv()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:///./centralign.db"
)

connect_args = (
    {"check_same_thread": False}
    if DATABASE_URL.startswith("sqlite")
    else {}
)

engine = create_engine(
    DATABASE_URL,
    echo=False,
    connect_args=connect_args,
)


def create_db_and_tables(target_engine=None) -> None:
    """Create tables and add compatible columns to existing databases."""
    database_engine = target_engine or engine
    if inspect(database_engine).has_table("task"):
        columns = {
            column["name"]
            for column in inspect(database_engine).get_columns("task")
        }
        if "owner_id" not in columns:
            with database_engine.begin() as connection:
                connection.execute(text(
                    "ALTER TABLE task ADD COLUMN owner_id VARCHAR NOT NULL DEFAULT 'default'"
                ))
    SQLModel.metadata.create_all(database_engine)
    if inspect(database_engine).has_table("approval"):
        columns = {
            column["name"]
            for column in inspect(database_engine).get_columns("approval")
        }
        additions = {
            "execution_inputs_json": "TEXT",
            "capability": "VARCHAR",
            "action_hash": "VARCHAR",
            "risk_json": "TEXT",
            "decided_by": "VARCHAR",
            "expires_at": "DATETIME",
        }
        with database_engine.begin() as connection:
            for name, column_type in additions.items():
                if name not in columns:
                    connection.execute(text(
                        f"ALTER TABLE approval ADD COLUMN {name} {column_type}"
                    ))


def get_session():
    """Yield a database session for API endpoints."""
    with Session(engine) as session:
        yield session
