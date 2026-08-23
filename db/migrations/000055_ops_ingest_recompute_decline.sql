-- #1781: terminal state for a product recompute the compressed-chunk write
-- guard can never accept. One row records one declined decision, keyed by the
-- evidence that produced it, so any NEW evidence (a fresh initial state or a
-- newer product mtime) reopens the decision by simply not matching.
--
-- product_mtime is DOUBLE PRECISION, not TIMESTAMPTZ, on purpose: it must round
-- trip os.stat().st_mtime bit-for-bit or the equality match fails on the next
-- tick and the retry loop comes back.
CREATE TABLE IF NOT EXISTS ops.ingest_recompute_decline (
  run_id          TEXT             NOT NULL,
  init_state_id   TEXT             NOT NULL,
  product_mtime   DOUBLE PRECISION NOT NULL,
  reason_code     TEXT             NOT NULL,
  detail          TEXT,
  declined_at     TIMESTAMPTZ      NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, init_state_id, product_mtime)
);
