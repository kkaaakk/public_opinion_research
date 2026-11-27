"""PostgreSQL fixtures for the phase boundary and physical-call ledger."""

import os
import re
from urllib.parse import urlsplit

import psycopg
from manager import RAW, BudgetExhausted, UnknownEffect
from psycopg import sql

APP = "por-manager-agent-poc"
VERSION = "manager-poc-v1"
EXECUTOR = "manager-poc-worker"
DATABASE_ENV = "POR_MANAGER_POC_DATABASE_URL"


def database_url():
    value = os.environ[DATABASE_ENV]
    url = urlsplit(value)
    if (
        url.scheme != "postgresql"
        or url.hostname != "127.0.0.1"
        or url.port is None
        or url.username != "por_manager_poc"
        or url.password is not None
        or url.path != "/por_manager_agent_poc"
        or url.query
        or url.fragment
    ):
        raise ValueError(
            f"{DATABASE_ENV} must identify the isolated loopback fixture database."
        )
    return value


class Lab:
    def __init__(self, schema):
        if not re.fullmatch(r"manager_poc_[a-f0-9]{12}", schema):
            raise ValueError("Invalid experiment schema name.")
        self.schema = schema
        self.journal = schema + "_journal"
        self.url = database_url()

    def connect(self):
        return psycopg.connect(
            self.url, options=f"-c search_path={self.schema}", connect_timeout=5
        )

    def create(self, cap):
        with self.connect() as conn:
            conn.execute(
                sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema))
            )
            conn.execute("""
                CREATE TABLE budget (cap int NOT NULL);
                CREATE TABLE model_attempt (
                    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    purpose text NOT NULL,
                    tokens int
                );
                CREATE TABLE evidence (id text PRIMARY KEY, value text NOT NULL);
                CREATE TABLE operation (id text PRIMARY KEY, reference bigint);
                CREATE TABLE external_effect (
                    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    operation_id text NOT NULL
                );
                CREATE TABLE observation (kind text NOT NULL);
            """)
            conn.execute("INSERT INTO budget VALUES (%s)", (cap,))

    def execute(self, query, *args):
        with self.connect() as conn:
            cursor = conn.execute(query, args)
            return cursor.fetchall() if cursor.description else []

    def observe(self, kind):
        self.execute("INSERT INTO observation VALUES (%s)", kind)

    def admit(self, purpose):
        with self.connect() as conn:
            (cap,) = conn.execute("SELECT cap FROM budget FOR UPDATE").fetchone()
            (count,) = conn.execute("SELECT count(*) FROM model_attempt").fetchone()
            if count >= (cap if purpose == "report" else cap - 1):
                raise BudgetExhausted(purpose)
            (attempt,) = conn.execute(
                "INSERT INTO model_attempt (purpose) VALUES (%s) RETURNING id",
                (purpose,),
            ).fetchone()
            return attempt

    def settle(self, attempt, usage):
        self.execute(
            "UPDATE model_attempt SET tokens = %s WHERE id = %s",
            usage.total_tokens,
            attempt,
        )

    def check_operations(self):
        unknown = self.execute("SELECT id FROM operation WHERE reference IS NULL")
        if unknown:
            raise UnknownEffect(f"Unknown result for {unknown[0][0]}")

    def reused_evidence(self, key):
        rows = self.execute("SELECT value FROM evidence WHERE id = %s", key)
        if rows:
            self.observe("evidence-reused:" + key)
            return rows[0][0]
        return None

    def admit_operation(self, key):
        with self.connect() as conn:
            new = conn.execute(
                "INSERT INTO operation (id) VALUES (%s) ON CONFLICT DO NOTHING RETURNING id",
                (key,),
            ).fetchone()
            if new:
                return None
            (reference,) = conn.execute(
                "SELECT reference FROM operation WHERE id = %s", (key,)
            ).fetchone()
            if reference is None:
                raise UnknownEffect(f"Unknown result for {key}")
            return reference

    def external_call(self, key):
        # A separate committed mock-provider transaction permits duplicate effects.
        [(reference,)] = self.execute(
            "INSERT INTO external_effect (operation_id) VALUES (%s) RETURNING id", key
        )
        return reference

    def external_lookup(self, key, reference):
        assert self.execute(
            "SELECT operation_id FROM external_effect WHERE id = %s", reference
        ) == [(key,)]
        self.observe("external-lookup:" + key)
        return RAW

    def store_evidence(self, key, evidence):
        self.execute(
            "INSERT INTO evidence VALUES (%s, %s) ON CONFLICT DO NOTHING", key, evidence
        )
