-- 009: 工具调用审计
-- 轨迹评测（第二层）的数据来源：工具名、参数、结果、耗时
CREATE TABLE tool_calls (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id     uuid NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
  tool_name  text NOT NULL,
  args       jsonb NOT NULL DEFAULT '{}',
  result     jsonb,
  status     text NOT NULL DEFAULT 'running'
             CHECK (status IN ('running', 'succeeded', 'failed', 'denied')),
  error      text,
  started_at timestamptz NOT NULL DEFAULT now(),
  ended_at   timestamptz
);

CREATE INDEX tool_calls_run_started_idx ON tool_calls (run_id, started_at);
CREATE INDEX tool_calls_tool_name_idx ON tool_calls (tool_name);
