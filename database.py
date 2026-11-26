import os
import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

load_dotenv(os.path.join(BASE_DIR, ".env"))

DATABASE_URL = os.getenv("DATABASE_URL")


class CursorAdapter:

    def __init__(self, cursor):
        self.cursor = cursor

    def execute(self, query, params=None):
        query = query.replace("?", "%s")
        self.cursor.execute(query, params)
        return self

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()

    @property
    def rowcount(self):
        return self.cursor.rowcount

    def close(self):
        self.cursor.close()


class ConnectionAdapter:

    def __init__(self, connection):
        self.connection = connection

    def cursor(self):
        return CursorAdapter(self.connection.cursor(cursor_factory=RealDictCursor))

    def execute(self, query, params=None):
        cursor = self.cursor()
        cursor.execute(query, params)
        return cursor

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()

    def close(self):
        self.connection.close()


def get_db_connection():

    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing from .env")

    connection = psycopg2.connect(DATABASE_URL, sslmode="require")

    return ConnectionAdapter(connection)


def initialize_database():

    connection = get_db_connection()

    try:
        connection.execute("SELECT 1")
        print("Supabase Database Ready!")

    finally:
        connection.close()


if __name__ == "__main__":

    initialize_database()
