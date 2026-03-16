"""Neo4j database client for graph-based similarity search."""

import os
from neo4j import GraphDatabase


class Neo4jClient:
    """Synchronous Neo4j driver wrapper."""

    def __init__(self):
        self._driver = None

    def connect(self):
        uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
        user = os.getenv("NEO4J_USER", "neo4j")
        password = os.getenv("NEO4J_PASSWORD", "changeme")
        self._driver = GraphDatabase.driver(uri, auth=(user, password))
        self._driver.verify_connectivity()

    def close(self):
        if self._driver:
            self._driver.close()
            self._driver = None

    @property
    def driver(self):
        return self._driver

    def execute_query(self, query: str, **params) -> list[dict]:
        """Execute a Cypher query and return results as list of dicts."""
        with self._driver.session() as session:
            result = session.run(query, **params)
            return [dict(record) for record in result]
