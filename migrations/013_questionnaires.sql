-- 013: 结构化追问 + 联网授权请求
-- 问卷 1–4 题、可取消、答案持久化、恢复时配对 toolCall/toolResult
CREATE TABLE questionnaires (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id      uuid NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
  thread_id   uuid NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  questions   jsonb NOT NULL,
  answers     jsonb,
  status      text NOT NULL DEFAULT 'pending'
              CHECK (status IN ('pending', 'answered', 'cancelled')),
  created_at  timestamptz NOT NULL DEFAULT now(),
  answered_at timestamptz
);

CREATE INDEX questionnaires_run_idx ON questionnaires (run_id);
CREATE INDEX questionnaires_thread_status_idx ON questionnaires (thread_id, status);

-- 联网授权（ask 模式下每次联网工具调用前暂停等待）
-- 授权后 run 恢复；grantedPermission 只放行一次
CREATE TABLE permission_requests (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id     uuid NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
  tool_name  text NOT NULL,
  args       jsonb NOT NULL DEFAULT '{}',
  status     text NOT NULL DEFAULT 'pending'
             CHECK (status IN ('pending', 'granted', 'denied')),
  created_at timestamptz NOT NULL DEFAULT now(),
  decided_at timestamptz
);

CREATE INDEX permission_requests_run_idx ON permission_requests (run_id);
CREATE INDEX permission_requests_status_idx ON permission_requests (status);
