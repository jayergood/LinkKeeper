-- 011: Action Card（提案与真实执行的边界）
-- 状态机 proposed→confirmed→executed/failed/conflict/rejected
-- version 是 Action 自身版本；base_version 是它要修改的目标对象版本（CAS 依据）
-- base_version_source 标明 baseVersion 由模型还是服务端填入
CREATE TABLE actions (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  thread_id           uuid NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  contact_id          uuid REFERENCES contacts(id) ON DELETE SET NULL,
  type                text NOT NULL CHECK (type IN (
                        'create_contact',
                        'update_contact',
                        'merge_contacts',
                        'create_event',
                        'update_event',
                        'delete_event'
                      )),
  payload             jsonb NOT NULL,
  status              text NOT NULL DEFAULT 'proposed' CHECK (status IN (
                        'proposed',
                        'confirmed',
                        'rejected',
                        'executed',
                        'failed',
                        'conflict'
                      )),
  -- Action 自身版本
  version             integer NOT NULL DEFAULT 1 CHECK (version > 0),
  -- 目标对象的版本（CAS 用）
  base_version        integer,
  base_version_source text CHECK (base_version_source IN ('model', 'server')),
  evidence_ids        uuid[] NOT NULL DEFAULT '{}',
  rationale           text,
  created_at          timestamptz NOT NULL DEFAULT now(),
  decided_at          timestamptz,
  executed_at         timestamptz
);

CREATE INDEX actions_thread_status_idx ON actions (thread_id, status);
CREATE INDEX actions_contact_status_idx ON actions (contact_id, status);
