from common import contracts as C
from contextlib import contextmanager
from datetime import datetime, timezone
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, JSON, CheckConstraint, inspect, create_engine, event, text, update
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

def now():
    return datetime.now(timezone.utc)

def iso(value):
    return value.replace(tzinfo=timezone.utc).isoformat() if value.tzinfo is None else value.astimezone(timezone.utc).isoformat()

class Base(DeclarativeBase):
    pass


class AIModel(Base):
    __tablename__ = "ai_models"
    model_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    artifact_uri: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class SanctionsList(Base):
    __tablename__ = "sanctions_lists"
    list_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    artifact_uri: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class Bundle(Base):
    __tablename__ = "bundles"
    bundle_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    model_id: Mapped[str] = mapped_column(ForeignKey("ai_models.model_id"))
    list_id: Mapped[str] = mapped_column(ForeignKey("sanctions_lists.list_id"))
    status: Mapped[str] = mapped_column(String(30), default="candidate")
    base_bundle_id: Mapped[str | None] = mapped_column(ForeignKey("bundles.bundle_id"))
    history: Mapped[list] = mapped_column(JSON, default=list)
    previous_active_id: Mapped[str | None] = mapped_column(ForeignKey("bundles.bundle_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (Index("uq_registry_active", "status", unique=True,
        postgresql_where=text("status = 'active'"), sqlite_where=text("status = 'active'")),
        Index("uq_registry_canary", "status", unique=True,
        postgresql_where=text("status = 'canary'"), sqlite_where=text("status = 'canary'")),)

class Import(Base):
    __tablename__ = "imports"
    __table_args__ = (CheckConstraint("model_id IS NULL OR list_id IS NULL", name="ck_import_single_asset"),)
    import_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    artifact_uri: Mapped[str | None] = mapped_column(String(500), unique=True)
    filename: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(30), default=C.IMPORT_APPROVED)
    reason: Mapped[str | None] = mapped_column(String(500))
    model_id: Mapped[str | None] = mapped_column(ForeignKey("ai_models.model_id"))
    list_id: Mapped[str | None] = mapped_column(ForeignKey("sanctions_lists.list_id"))
    bundle_id: Mapped[str | None] = mapped_column(ForeignKey("bundles.bundle_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

class WorkflowLock(Base):
    __tablename__ = "registry_lock"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)

class Database:
    def __init__(self, url):
        args = {"check_same_thread": False, "timeout": 30} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, connect_args=args, pool_pre_ping=True)
        if url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def foreign_keys(conn, _):
                conn.execute("PRAGMA foreign_keys=ON")
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    def check_schema(self):
        """Read-only readiness check. D applies versioned migrations separately."""
        inspector = inspect(self.engine)
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                raise RuntimeError(f"Missing {table.name}; apply the B registry migration first")
            actual = {column["name"] for column in inspector.get_columns(table.name)}
            if actual != set(table.columns.keys()):
                raise RuntimeError(f"Legacy {table.name} schema detected; apply the B registry migration first")
        indexes = {index["name"] for index in inspector.get_indexes("bundles") if index["unique"]}
        if not {"uq_registry_active", "uq_registry_canary"} <= indexes:
            raise RuntimeError("Missing deployment uniqueness constraints; apply the B registry migration first")
        with self.engine.connect() as conn:
            if conn.scalar(text("SELECT id FROM registry_lock WHERE id=1")) != 1:
                raise RuntimeError("Missing registry lock; apply the B registry migration first")

    @contextmanager
    def transaction(self):
        with self.sessions.begin() as session:
            result = session.execute(update(WorkflowLock).where(WorkflowLock.id == 1).values(id=WorkflowLock.id))
            if result.rowcount != 1:
                raise RuntimeError("Apply the B registry migration first")
            yield session
