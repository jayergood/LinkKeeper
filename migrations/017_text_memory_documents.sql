-- 017: 长期记忆文档（按联系人、按版本存储）
-- content 不超过 8000 字符（业务层校验）
-- 每个联系人最多一份 active；新版本必须带 based_on_version 走 CAS
CREATE TABLE memory_documents (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  contact_id       uuid NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
  version          integer NOT NULL CHECK (version > 0),
  content_markdown text NOT NULL,
  status           text NOT NULL DEFAULT 'draft'
                   CHECK (status IN ('draft', 'active', 'archived')),
  -- 这份草案基于哪个 active 版本生成；审核时用它做 CAS
  based_on_version integer,
  evidence_ids     uuid[] NOT NULL DEFAULT '{}',
  approved_at      timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),

  UNIQUE (contact_id, version)
);

-- 每个联系人最多一份 active 文档（部分唯一索引）；历史版本归档保留
CREATE UNIQUE INDEX memory_documents_one_active_idx
  ON memory_documents (contact_id)
  WHERE status = 'active';

CREATE INDEX memory_documents_contact_status_idx ON memory_documents (contact_id, status);
