-- 012: 执行回执（confirm ≠ receipt）
-- 回执独立于 Action 状态：证明"系统实际写了什么"
-- action_id UNIQUE：一个 Action 最多一个回执，重复 confirm 靠此唯一约束兜底
CREATE TABLE action_receipts (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  action_id     uuid NOT NULL UNIQUE REFERENCES actions(id) ON DELETE CASCADE,
  status        text NOT NULL CHECK (status IN ('success', 'failed')),
  actual_result jsonb NOT NULL DEFAULT '{}',
  error         text,
  receipt_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX action_receipts_status_idx ON action_receipts (status);
