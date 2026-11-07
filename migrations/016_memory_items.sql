-- 016: 候选/已确认记忆条目
-- 候选记忆必须带 evidence_ids（业务层校验，无来源直接拒绝）
-- candidate → 用户确认 → confirmed；模型不能代替用户确认
-- Insight 附带的 memoryDraft 落在这里，由 source_insight_id 反查来源
CREATE TABLE memory_items (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  contact_id        uuid NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
  kind              text NOT NULL,
  content           text NOT NULL,
  status            text NOT NULL DEFAULT 'candidate'
                    CHECK (status IN ('candidate', 'confirmed', 'dismissed', 'superseded')),
  confidence        text NOT NULL DEFAULT 'medium'
                    CHECK (confidence IN ('low', 'medium', 'high')),
  evidence_ids      uuid[] NOT NULL DEFAULT '{}',
  source_insight_id uuid REFERENCES insights(id) ON DELETE SET NULL,
  superseded_by     uuid REFERENCES memory_items(id) ON DELETE SET NULL,
  created_at        timestamptz NOT NULL DEFAULT now(),
  confirmed_at      timestamptz
);

CREATE INDEX memory_items_contact_status_idx ON memory_items (contact_id, status);
CREATE INDEX memory_items_source_insight_idx ON memory_items (source_insight_id);
