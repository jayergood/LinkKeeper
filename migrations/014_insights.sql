-- 014: 行动后洞察（推断，不是事实）
-- 回执后触发，0~1 条
-- source_ids 支持 ev_/act_/msg_ 三类来源
-- 内容边界由 prompt 约束（不解释感情/人格/隐藏动机）
-- 部分唯一索引：一个 Action 对同一人最多一条洞察
CREATE TABLE insights (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  contact_id   uuid NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
  action_id    uuid REFERENCES actions(id) ON DELETE SET NULL,
  content      text NOT NULL,
  confidence   text NOT NULL CHECK (confidence IN ('low', 'medium', 'high')),
  status       text NOT NULL DEFAULT 'active'
               CHECK (status IN ('active', 'refuted', 'superseded')),
  evidence_ids uuid[] NOT NULL DEFAULT '{}',
  source_ids   text[] NOT NULL DEFAULT '{}',
  recheck_at   timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  refuted_at   timestamptz
);

-- 一个 Action 对同一个人最多一条洞察（多联系人 Action 按人各存一条）
CREATE UNIQUE INDEX insights_action_contact_idx
  ON insights (action_id, contact_id)
  WHERE action_id IS NOT NULL;

CREATE INDEX insights_contact_status_idx ON insights (contact_id, status);
CREATE INDEX insights_recheck_idx ON insights (recheck_at) WHERE status = 'active';
