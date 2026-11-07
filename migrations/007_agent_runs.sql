-- 007: Agent 运行记录（一次请求 = 一个 run）
-- UNIQUE(thread_id, request_id) = 请求幂等
-- 状态机：running/waiting_*/completed/proposed/failed/aborted
-- resumed_from_run_id 记录从哪次 run 恢复（问卷/授权）
CREATE TABLE agent_runs (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  thread_id           uuid NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  request_id          text NOT NULL,
  status              text NOT NULL DEFAULT 'running' CHECK (status IN (
                        'running',
                        'waiting_questionnaire',
                        'waiting_permission',
                        'completed',
                        'proposed',
                        'failed',
                        'aborted'
                      )),
  model               text NOT NULL,
  prompt_version      text,
  permission_mode     text NOT NULL DEFAULT 'ask' CHECK (permission_mode IN ('ask', 'full')),
  page_context        jsonb NOT NULL DEFAULT '{}',
  budget              jsonb NOT NULL DEFAULT '{}',
  usage               jsonb NOT NULL DEFAULT '{}',
  resumed_from_run_id uuid REFERENCES agent_runs(id) ON DELETE SET NULL,
  error               text,
  started_at          timestamptz NOT NULL DEFAULT now(),
  ended_at            timestamptz,

  UNIQUE (thread_id, request_id)
);

CREATE INDEX agent_runs_thread_started_idx ON agent_runs (thread_id, started_at DESC);
CREATE INDEX agent_runs_status_idx ON agent_runs (status);
