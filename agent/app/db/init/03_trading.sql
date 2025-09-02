-- live orders we place
CREATE TABLE IF NOT EXISTS mexc_orders (
  id            BIGSERIAL PRIMARY KEY,
  symbol        VARCHAR(20) NOT NULL,
  side          VARCHAR(4)  NOT NULL,         -- BUY/SELL
  type          VARCHAR(10) NOT NULL,         -- LIMIT, etc
  client_order_id VARCHAR(64),
  mexc_order_id BIGINT,
  price         DOUBLE PRECISION,
  qty           DOUBLE PRECISION,
  status        VARCHAR(24),                  -- NEW/PARTIALLY_FILLED/FILLED/CANCELED/REJECTED
  is_test       BOOLEAN NOT NULL DEFAULT true,
  error         TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mexc_orders_sym ON mexc_orders(symbol);
CREATE INDEX IF NOT EXISTS idx_mexc_orders_status ON mexc_orders(status);

-- one row per symbol position we manage (spot only)
CREATE TABLE IF NOT EXISTS positions (
  symbol       VARCHAR(20) PRIMARY KEY,
  qty          DOUBLE PRECISION NOT NULL DEFAULT 0,
  avg_price    DOUBLE PRECISION,
  state        VARCHAR(16) NOT NULL DEFAULT 'flat', -- flat|long|closing
  target_price DOUBLE PRECISION,                    -- TP limit
  stop_price   DOUBLE PRECISION,                    -- SL limit (optional)
  last_buy_order BIGINT,                            -- mexc order id (buy)
  last_sell_order BIGINT,                           -- mexc order id (tp)
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
