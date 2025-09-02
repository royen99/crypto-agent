-- Per-symbol time series for recommendations
CREATE TABLE IF NOT EXISTS rec_points (
  id           BIGSERIAL PRIMARY KEY,
  as_of        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  symbol       VARCHAR(20) NOT NULL,
  interval     VARCHAR(8)  NOT NULL,
  price        DOUBLE PRECISION,
  score        DOUBLE PRECISION,
  rsi14        DOUBLE PRECISION,
  macd_hist    DOUBLE PRECISION,
  change24h    DOUBLE PRECISION,
  recommendation VARCHAR(16),
  reasons      JSONB NOT NULL DEFAULT '[]'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_rec_points_sym_int_time
  ON rec_points(symbol, interval, as_of DESC);

-- (optional) quick filters
CREATE INDEX IF NOT EXISTS idx_rec_points_time ON rec_points(as_of DESC);
