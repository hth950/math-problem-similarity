import os
from dotenv import load_dotenv

load_dotenv()

# Database
DB_HOST = os.getenv("OCI_DB_PROD_HOST", "localhost")
DB_PORT = int(os.getenv("OCI_DB_PROD_PORT", "33108"))
DB_USER = os.getenv("OCI_DB_PROD_USER", "")
DB_PASSWORD = os.getenv("OCI_DB_PROD_PASSWORD", "")
DB_NAME = os.getenv("OCI_DB_PROD_NAME", "problem_bank")

# OpenAI Embedding
OPENAI_API_KEY_EMBEDDING = os.getenv("OPENAI_API_KEY_EMBEDDING", "")

# Dev LLM (GPT-OSS)
DEV_LLM_URL = os.getenv("DEV_LLM_URL", "")
DEV_LLM_KEY = os.getenv("DEV_LLM_KEY", "")
DEV_LLM_NAME = os.getenv("DEV_LLM_NAME", "")

# OpenRouter (Gemini)
OPENROUTER_URL = "https://openrouter.ai/api/v1"
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "")

# Neo4j
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "changeme")
