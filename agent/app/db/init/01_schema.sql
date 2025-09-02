-- 01_schema.sql  (Autonomous Agent MVP)
-- Creates enum, tables, triggers, and indexes.

-- ===== ENUMS =====
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'runstatus') THEN
    CREATE TYPE runstatus AS ENUM ('queued','running','success','error','stopped');
  END IF;
END$$;

-- ===== FUNCTIONS =====
-- Generic “updated_at” updater for tables that have that column
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
  NEW.updated_at := NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Specific updater for memories.last_seen
CREATE OR REPLACE FUNCTION set_last_seen() RETURNS trigger AS $$
BEGIN
  NEW.last_seen := NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ===== TABLES =====
-- Runs: one row per agent run
CREATE TABLE IF NOT EXISTS runs (
  id            VARCHAR(36) PRIMARY KEY,
  goal          TEXT NOT NULL,
  status        runstatus NOT NULL DEFAULT 'queued',
  final_answer  TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Events: step-by-step thoughts/tools/observations
CREATE TABLE IF NOT EXISTS run_events (
  id         BIGSERIAL PRIMARY KEY,
  run_id     VARCHAR(36) NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  step       INTEGER NOT NULL DEFAULT 0,
  type       VARCHAR(32) NOT NULL,          -- thought | tool | observation | final | error | log
  content    JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Long-term memory store
CREATE TABLE IF NOT EXISTS memories (
  id        BIGSERIAL PRIMARY KEY,
  key       VARCHAR(128) NOT NULL,
  value     TEXT NOT NULL,
  tags      JSONB NOT NULL DEFAULT '[]'::jsonb,
  last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Recommendation snapshots
CREATE TABLE IF NOT EXISTS rec_snapshots (
  id BIGSERIAL PRIMARY KEY,
  as_of TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  interval VARCHAR(8) NOT NULL,
  payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rec_snapshots_asof ON rec_snapshots(as_of);

-- ===== TRIGGERS =====
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger WHERE tgname = 'trg_runs_updated_at'
  ) THEN
    CREATE TRIGGER trg_runs_updated_at
      BEFORE UPDATE ON runs
      FOR EACH ROW EXECUTE FUNCTION set_updated_at();
  END IF;
END$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger WHERE tgname = 'trg_memories_last_seen'
  ) THEN
    CREATE TRIGGER trg_memories_last_seen
      BEFORE UPDATE ON memories
      FOR EACH ROW EXECUTE FUNCTION set_last_seen();
  END IF;
END$$;

-- ===== INDEXES =====
CREATE INDEX IF NOT EXISTS idx_runs_status            ON runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_created_at        ON runs(created_at);

CREATE INDEX IF NOT EXISTS idx_events_run_id          ON run_events(run_id);
CREATE INDEX IF NOT EXISTS idx_events_run_step        ON run_events(run_id, step, id);
CREATE INDEX IF NOT EXISTS idx_events_created_at      ON run_events(created_at);
CREATE INDEX IF NOT EXISTS idx_events_content_gin     ON run_events USING GIN (content);

CREATE INDEX IF NOT EXISTS idx_memories_key           ON memories(key);
CREATE INDEX IF NOT EXISTS idx_memories_tags_gin      ON memories USING GIN (tags);
