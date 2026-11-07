-- 015: 关系目标（活跃目标会进入 MemorySnapshot）
CREATE TABLE relationship_goals (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  contact_id   uuid NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
  description  text NOT NULL,
  status       text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
  evidence_ids uuid[] NOT NULL DEFAULT '{}',
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX relationship_goals_contact_status_idx ON relationship_goals (contact_id, status);
