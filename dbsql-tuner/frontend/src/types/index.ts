export interface OracleInstance {
  id: number;
  name: string;
  host: string;
  port: number;
  service_name: string;
  read_user: string;
  sandbox_user?: string | null;
  oracle_version?: string | null;
  status: string;
  last_checked?: string | null;
  created_at: string;
  updated_at: string;
}

export interface TopSQLRow {
  sql_id?: string | null;
  hash_value?: number | null;
  child_number: number;
  sql_text?: string | null;
  executions?: number | null;
  elapsed_time_us?: number | null;
  cpu_time_us?: number | null;
  buffer_gets?: number | null;
  disk_reads?: number | null;
  module?: string | null;
  source: string;
  avg_elapsed_ms?: number | null;
}

export interface ConnectionTestResult {
  ok: boolean;
  oracle_version?: string | null;
  message: string;
  permissions: string[];
  missing_permissions: string[];
}
