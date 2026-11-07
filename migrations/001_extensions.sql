-- 001: 基础扩展
-- gen_random_uuid() 需要 pgcrypto（PG13+ 有内置版本，但显式装扩展兼容更广）
CREATE EXTENSION IF NOT EXISTS pgcrypto;
