
import os

from dotenv import load_dotenv
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


def create_db_and_tables() -> None:
    """Create database tables registered with SQLModel."""
    SQLModel.metadata.create_all(engine)


def get_session():
    """Yield a database session for API endpoints."""
    with Session(engine) as session:
        yield session
