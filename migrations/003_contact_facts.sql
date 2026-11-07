-- 003: 联系人结构化事实
-- status 区分 candidate/confirmed/rejected/superseded
-- evidence_ids 让每条事实都能追溯到证据
CREATE TABLE contact_facts (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  contact_id    uuid NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
  field         text NOT NULL,
  value         text NOT NULL,
  status        text NOT NULL DEFAULT 'confirmed'
                CHECK (status IN ('candidate', 'confirmed', 'dismissed', 'superseded')),
  confidence    text NOT NULL DEFAULT 'medium'
                CHECK (confidence IN ('low', 'medium', 'high')),
  evidence_ids  uuid[] NOT NULL DEFAULT '{}',
  -- 被哪条新事实替代；保留旧行用于追溯，绝不物理删除
  superseded_by uuid REFERENCES contact_facts(id) ON DELETE SET NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX contact_facts_contact_field_idx ON contact_facts (contact_id, field, status);
CREATE INDEX contact_facts_status_idx ON contact_facts (status);
