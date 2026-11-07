-- 018: 会话压缩截止位点
-- 记录已压进 summary 的最后一条消息 seq
-- 下一次加载只取 seq 更大的消息，避免每次 run 都重复压缩同一段历史
ALTER TABLE threads
  ADD COLUMN compaction_until_seq integer NOT NULL DEFAULT 0
  CHECK (compaction_until_seq >= 0);
