-- 008: Agent 事件流（SSE 推送 + 断线回放）
-- seq 单调递增；客户端重连可用 after=seq 续传
CREATE TABLE agent_events (
  id         bigserial PRIMARY KEY,
  run_id     uuid NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
  seq        integer NOT NULL CHECK (seq >= 1),
  type       text NOT NULL,
  payload    jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),

  UNIQUE (run_id, seq)
);

CREATE INDEX agent_events_run_seq_idx ON agent_events (run_id, seq);
