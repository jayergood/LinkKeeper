-- 006: 会话消息（幂等重放的基础）
-- seq 在同一 thread 内单调递增；恢复时按 seq 排序，不依赖时间戳
CREATE TABLE messages (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  thread_id  uuid NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  seq        integer NOT NULL CHECK (seq >= 1),
  role       text NOT NULL,
  payload    jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),

  UNIQUE (thread_id, seq)
);

CREATE INDEX messages_thread_seq_idx ON messages (thread_id, seq);
