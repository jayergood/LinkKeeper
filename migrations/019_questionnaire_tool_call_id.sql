-- 019: 问卷记录模型的 tool_call_id
-- 恢复时必须知道当初调用 questionnaire 用的是哪个 tool_call_id，
-- 否则补出的 toolResult 无法与那条 assistant 消息对应。
-- 允许 NULL：旧数据或手工创建的问卷没有这个 id，恢复流程会退化为只用 user 消息推进。
ALTER TABLE questionnaires
  ADD COLUMN tool_call_id text;

COMMENT ON COLUMN questionnaires.tool_call_id IS
  '模型调用 questionnaire 时的 tool_call_id；恢复时用于补齐配对的 toolResult';
