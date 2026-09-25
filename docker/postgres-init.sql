-- Runs once on first cluster init (docker-entrypoint-initdb.d).
CREATE EXTENSION IF NOT EXISTS vector;
