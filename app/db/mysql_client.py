import mysql.connector
from mysql.connector import pooling


class MySQLClient:
    def __init__(self):
        self._pool = None

    def _get_pool(self):
        if self._pool is None:
            from app.config import DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME
            self._pool = pooling.MySQLConnectionPool(
                pool_name='similarity_pool',
                pool_size=10,
                host=DB_HOST,
                port=DB_PORT,
                user=DB_USER,
                password=DB_PASSWORD,
                database=DB_NAME,
            )
        return self._pool

    def execute_query(self, query: str, params: tuple | None = None) -> list[dict]:
        conn = self._get_pool().get_connection()
        cursor = None
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(query, params)
            return cursor.fetchall()
        finally:
            if cursor:
                cursor.close()
            conn.close()

    def fetch_problem_by_id(self, problem_id: int) -> dict | None:
        query = """
        SELECT p.id, p.group_id, p.question, p.refer, p.answer, p.solution,
               p.choice1, p.choice2, p.choice3, p.choice4, p.choice5,
               p.grade, p.school_level, p.type, p.level,
               p.tag_ids, p.main_category_tag_id
        FROM problem p
        WHERE p.id = %s
        """
        rows = self.execute_query(query, (problem_id,))
        return rows[0] if rows else None

    def fetch_problems_with_metadata(self, limit: int = 10000) -> list[dict]:
        """수학 문제 + 메타데이터 추출 (is_serving=1, subject=math)"""
        query = """
        SELECT p.id, p.group_id, p.question, p.refer, p.answer, p.solution,
               p.choice1, p.choice2, p.choice3, p.choice4, p.choice5,
               p.grade, p.school_level, p.type, p.level,
               p.tag_ids, p.main_category_tag_id,
               pg.instruction, si.name as source_name, si.exam_type, si.year,
               sc.kor_name as school_name, sc.city
        FROM problem p
        LEFT JOIN problem_group pg ON pg.id = p.group_id
        LEFT JOIN source_info si ON si.id = pg.source_id
        LEFT JOIN school sc ON sc.id = si.school_id
        WHERE p.subject = 'math' AND p.is_serving = 1
        ORDER BY RAND()
        LIMIT %s
        """
        return self.execute_query(query, (limit,))

    def search_problems_by_ids(self, ids: list[int]) -> list[dict]:
        """여러 problem ID로 문제 조회"""
        if not ids:
            return []
        placeholders = ','.join(['%s'] * len(ids))
        query = f"""
        SELECT p.id, p.group_id, p.question, p.refer, p.answer, p.solution,
               p.choice1, p.choice2, p.choice3, p.choice4, p.choice5,
               p.grade, p.school_level, p.type, p.level,
               p.tag_ids, p.main_category_tag_id
        FROM problem p
        WHERE p.id IN ({placeholders})
        """
        return self.execute_query(query, tuple(ids))
