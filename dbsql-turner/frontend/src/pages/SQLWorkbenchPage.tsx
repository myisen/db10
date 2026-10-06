import { useEffect, useState } from 'react';
import { App, Button, Card, Col, Descriptions, Input, Row, Segmented, Space, Table, Tag, Typography, Divider, Alert, Badge, List } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import api from '../services/api';
import type { OracleInstance } from '../types';

const { Title, Text } = Typography;
const { TextArea } = Input;

const SAMPLE_SQL = `SELECT o.order_id, o.order_date, c.customer_name,
       SUM(oi.quantity * oi.price) AS total
FROM orders o, customers c, order_items oi
WHERE o.customer_id = c.customer_id
  AND o.order_id = oi.order_id
  AND o.order_date > SYSDATE - 30
GROUP BY o.order_id, o.order_date, c.customer_name
ORDER BY o.order_date DESC`;

// ---------------------------------------------------------------------------
// Types (mirror Pydantic schemas from /api/v1/...)
// ---------------------------------------------------------------------------
interface DepOut { owner?: string | null; name: string; object_type: string; }
interface TableProfileOut {
  owner: string; table_name: string;
  num_rows?: number | null; blocks?: number | null; empty_blocks?: number | null;
  avg_row_len?: number | null; pct_free?: number | null;
  last_analyzed?: string | null; partitioned?: string | null; num_partitions?: number | null;
  stale: boolean; staleness_reason?: string | null;
  inserts?: number | null; updates?: number | null; deletes?: number | null;
}
interface IndexColumnOut { column_name: string; column_position: number; descend?: string | null; }
interface IndexProfileOut {
  owner: string; index_name: string; table_name: string;
  uniqueness?: string | null; index_type?: string | null;
  leaf_blocks?: number | null; distinct_keys?: number | null;
  clustering_factor?: number | null; num_rows?: number | null;
  usage_monitoring?: boolean | null; used?: boolean | null;
  column_count: number; columns: IndexColumnOut[];
}
interface ColumnProfileOut {
  owner: string; table_name: string; column_name: string;
  data_type?: string | null; data_length?: number | null; nullable?: string | null;
  num_nulls?: number | null; num_distinct?: number | null;
  histogram?: string | null; histogram_buckets?: number | null;
}
interface ObjectProfileOut {
  instance_id: number; deps: DepOut[];
  tables: TableProfileOut[]; indexes: IndexProfileOut[]; columns: ColumnProfileOut[];
}
interface PlanNodeOut {
  id: number; parent_id?: number | null; operation: string; options?: string | null;
  object_owner?: string | null; object_name?: string | null;
  rows?: number | null; bytes?: number | null; cost?: number | null;
  time?: number | null; level: number;
  access_predicates?: string | null; filter_predicates?: string | null;
}
interface ActualPlanNodeOut extends PlanNodeOut {
  e_rows?: number | null; a_rows?: number | null;
  buffers?: number | null; reads?: number | null; temp_spc?: number | null;
  deviation?: number | null; row_estimation_bad: boolean;
}
interface ExecutionPlanOut {
  estimated_plan: PlanNodeOut[]; actual_plan: ActualPlanNodeOut[];
  display_text?: string | null; source: string;
  sql_id?: string | null; child_number?: number | null; oracle_version?: string | null;
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------
export default function SQLWorkbenchPage() {
  const { message } = App.useApp();
  const [instances, setInstances] = useState<OracleInstance[]>([]);
  const [instanceId, setInstanceId] = useState<number | null>(null);
  const [sql, setSql] = useState(SAMPLE_SQL);
  const [loadingProfile, setLoadingProfile] = useState(false);
  const [loadingPlan, setLoadingPlan] = useState(false);
  const [profile, setProfile] = useState<ObjectProfileOut | null>(null);
  const [plan, setPlan] = useState<ExecutionPlanOut | null>(null);
  const [tab, setTab] = useState<'tables' | 'indexes' | 'columns' | 'diagnose' | 'aux'>('tables');
  const [findingReport, setFindingReport] = useState<any | null>(null);
  const [loadingDiagnose, setLoadingDiagnose] = useState(false);
  const [auxSnapshot, setAuxSnapshot] = useState<any>(null);
  const [loadingAux, setLoadingAux] = useState(false);

  useEffect(() => {
    api.get('/instances').then(r => {
      setInstances(r.data);
      if (r.data.length > 0) setInstanceId(r.data[0].id);
    }).catch(() => {});
  }, []);

  const runAll = async () => {
    if (!instanceId) { message.warning('请选择 Oracle 实例'); return; }
    if (!sql.trim()) { message.warning('请输入 SQL'); return; }
    setLoadingProfile(true); setLoadingPlan(true); setLoadingDiagnose(true);
    try {
      const [pRes, eRes, dRes] = await Promise.allSettled([
        api.post('/object-profile', { instance_id: instanceId, sql }),
        api.post('/execution-plan', { instance_id: instanceId, sql }),
        api.post('/diagnose', { instance_id: instanceId, sql }),
      ]);
      if (pRes.status === 'fulfilled') setProfile(pRes.value.data);
      else message.error('对象画像: ' + (pRes.reason?.response?.data?.error || pRes.reason?.message));
      if (eRes.status === 'fulfilled') setPlan(eRes.value.data);
      else message.error('执行计划: ' + (eRes.reason?.response?.data?.error || eRes.reason?.message));
      if (dRes.status === 'fulfilled') setFindingReport(dRes.value.data);
      else message.error('诊断: ' + (dRes.reason?.response?.data?.detail || dRes.reason?.message));
    } finally {
      setLoadingProfile(false); setLoadingPlan(false); setLoadingDiagnose(false);
    }
  };

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 16 }}>
        <Title level={4} style={{ margin: 0 }}>SQL 工作台 · 对象画像 + 执行计划</Title>
        <Space>
          <Text type="secondary">Oracle 实例</Text>
          <Segmented
            value={instanceId}
            onChange={(v) => setInstanceId(Number(v))}
            options={instances.map(i => ({ label: `${i.name} (${i.host})`, value: i.id }))}
          />
          <Button type="primary" onClick={runAll} loading={loadingProfile || loadingPlan}>一键分析</Button>
        </Space>
      </div>

      <Row gutter={16}>
        {/* Left: SQL input + Execution plan */}
        <Col span={14}>
          <Card title="SQL 输入" style={{ marginBottom: 16 }}>
            <TextArea
              value={sql} onChange={(e) => setSql(e.target.value)}
              rows={6} style={{ fontFamily: 'Menlo, Consolas, monospace', fontSize: 12 }}
              placeholder="粘贴你要分析的 SQL..."
            />
          </Card>

          <Card title={
            <Space>
              <span>执行计划</span>
              {plan?.oracle_version && <Tag color="blue">Oracle {plan.oracle_version}</Tag>}
              {plan?.source === 'explain' && <Tag>EXPLAIN PLAN</Tag>}
              {plan?.source === 'sql_cursor' && <Tag color="green">V$SQL 实际计划</Tag>}
              {plan?.source === 'mixed' && <Tag color="purple">混合</Tag>}
            </Space>
          } extra={<Button size="small" onClick={runAll} loading={loadingPlan}>重新获取</Button>}>
            {plan ? (
              <PlanTree plan={plan} />
            ) : (
              <Text type="secondary">点击"一键分析"后此处显示执行计划树</Text>
            )}
            {plan?.display_text && (
              <>
                <Divider orientation="left">DBMS_XPLAN 原始文本</Divider>
                <pre style={{ fontSize: 11, background: '#0f172a', color: '#e2e8f0', padding: 12, borderRadius: 4, maxHeight: 280, overflow: 'auto', margin: 0 }}>
                  {plan.display_text}
                </pre>
              </>
            )}
          </Card>
        </Col>

        {/* Right: Object Profile */}
        <Col span={10}>
          <Card
            title={
              <Space>
                <span>对象画像 & 诊断</span>
                {profile && <Tag>{profile.deps.length} 个依赖对象</Tag>}
                {findingReport?.findings?.length > 0 && (
                  <Tag color="red"><Badge status="error" /> {findingReport.findings.length} 个诊断问题</Tag>
                )}
              </Space>
            }
            tabList={[
              { key: 'tables', tab: `表 (${profile?.tables.length ?? 0})` },
              { key: 'indexes', tab: `索引 (${profile?.indexes.length ?? 0})` },
              { key: 'columns', tab: `列 (${profile?.columns.length ?? 0})` },
              {
                key: 'diagnose',
                tab: (
                  <Space size={4}>
                    <span>诊断报告</span>
                    {findingReport?.findings?.length > 0 && (
                      <span style={{ background: '#ff4d4f', color: '#fff', borderRadius: 10, padding: '0 6px', fontSize: 11 }}>
                        {findingReport.findings.length}
                      </span>
                    )}
                  </Space>
                ),
              },
            ,
              { key: 'aux', tab: '辅助诊断' }
            ]}
            activeTabKey={tab}
            onTabChange={async (k) => {
            setTab(k as any);
            if (k === 'aux' && instanceId) {
              setLoadingAux(true);
              try {
                const resp = await fetch(`/api/v1/aux/snapshot?instance_id=${instanceId}`);
                if (resp.ok) setAuxSnapshot(await resp.json());
                else setAuxSnapshot({ error: (await resp.json()).detail || 'aux not available' });
              } catch (e: any) {
                setAuxSnapshot({ error: e.message });
              } finally { setLoadingAux(false); }
            }
          }}
            loading={loadingProfile || loadingDiagnose || loadingAux}
          >
            {!profile && tab !== 'diagnose' && <Text type="secondary">点击"一键分析"后此处显示对象画像</Text>}
            {profile && tab === 'tables' && <TablesPanel tables={profile.tables} />}
            {profile && tab === 'indexes' && <IndexesPanel indexes={profile.indexes} tables={profile.tables} />}
            {profile && tab === 'columns' && <ColumnsPanel columns={profile.columns} />}
            {tab === 'diagnose' && <DiagnosisPanel report={findingReport} />}
            {tab === 'aux' && <AuxiliaryPanel snapshot={auxSnapshot} loading={loadingAux} instanceId={instanceId} />}
          </Card>
        </Col>
      </Row>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Execution plan tree — indent by level, color by cost, highlight bad estimations
// ---------------------------------------------------------------------------
function PlanTree({ plan }: { plan: ExecutionPlanOut }) {
  const estimated = plan.estimated_plan || [];
  const actual = plan.actual_plan || [];

  if (estimated.length === 0 && actual.length === 0) {
    return <Alert type="info" message="无执行计划数据" showIcon />;
  }

  const cols: ColumnsType<any> = [
    {
      title: 'Operation', key: 'op', width: 380,
      render: (_, r: any) => {
        const indent = ' '.repeat(r.level || 0);
        const costColor = r.cost != null
          ? (r.cost > 5000 ? '#f5222d' : r.cost > 1000 ? '#fa8c16' : '#389e0d')
          : '#333';
        return (
          <div style={{ paddingLeft: indent.length * 6, lineHeight: 1.9 }}>
            <strong style={{ color: costColor }}>{r.operation}</strong>
            {r.options && <Text type="secondary" style={{ marginLeft: 4 }}>{r.options}</Text>}
            {r.object_name && (
              <Tag color="blue" style={{ marginLeft: 8 }}>
                {r.object_owner ? `${r.object_owner}.` : ''}{r.object_name}
              </Tag>
            )}
            {r.row_estimation_bad && (
              <Tag color="red" style={{ marginLeft: 8 }}>⚠ 基数偏差</Tag>
            )}
          </div>
        );
      },
    },
    { title: 'Cost', dataIndex: 'cost', key: 'cost', width: 100, render: (v) => v != null ? v.toFixed(1) : '-' },
    { title: '估算 Rows', key: 'e_rows', width: 120, render: (_, r: any) => r.rows != null ? r.rows.toLocaleString() : (r.e_rows != null ? r.e_rows.toLocaleString() : '-') },
    { title: '实际 Rows', key: 'a_rows', width: 120, render: (_, r: any) => r.a_rows != null ? r.a_rows.toLocaleString() : '-' },
    { title: 'Buffers', key: 'buffers', width: 100, render: (_, r: any) => r.buffers != null ? r.buffers.toLocaleString() : '-' },
    {
      title: '谓词', key: 'preds', width: 300,
      render: (_, r: any) => (
        <div style={{ fontSize: 11 }}>
          {r.access_predicates && <div><Text type="secondary">ACCESS:</Text> {r.access_predicates}</div>}
          {r.filter_predicates && <div><Text type="secondary">FILTER:</Text> {r.filter_predicates}</div>}
        </div>
      ),
    },
  ];

  // Merge estimated + actual into one display (use estimated as base, join on id)
  const actualMap = new Map(actual.map(n => [n.id, n]));
  const merged = estimated.map(e => {
    const a = actualMap.get(e.id);
    return {
      ...e,
      a_rows: a?.a_rows,
      buffers: a?.buffers,
      reads: a?.reads,
      temp_spc: a?.temp_spc,
      row_estimation_bad: a?.row_estimation_bad,
    };
  });
  // If estimated is empty (only actual from V$SQL), use actual nodes directly
  const rows = merged.length > 0 ? merged : actual;

  return (
    <Table
      size="small" bordered
      pagination={false}
      columns={cols}
      dataSource={rows as any[]}
      rowKey="id"
      style={{ fontSize: 12 }}
      scroll={{ x: 1200, y: 500 }}
    />
  );
}

// ---------------------------------------------------------------------------
// Object Profile panels
// ---------------------------------------------------------------------------
function TablesPanel({ tables }: { tables: TableProfileOut[] }) {
  if (tables.length === 0) return <Alert type="warning" message="本次 SQL 未解析到表依赖" showIcon />;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {tables.map((t, i) => (
        <Card key={i} size="small" title={
          <Space>
            <Tag color="geekblue">{t.owner}</Tag>
            <strong>{t.table_name}</strong>
            {t.stale && <Tag color="red">⚠ 统计陈旧</Tag>}
            {t.partitioned === 'YES' && <Tag color="purple">分区表 ({t.num_partitions})</Tag>}
          </Space>
        }>
          <Descriptions column={2} size="small" bordered>
            <Descriptions.Item label="行数 (NUM_ROWS)">{t.num_rows?.toLocaleString() ?? '-'}</Descriptions.Item>
            <Descriptions.Item label="块数 / 空块">
              {t.blocks?.toLocaleString() ?? '-'} / {t.empty_blocks ?? '-'}
            </Descriptions.Item>
            <Descriptions.Item label="平均行长">{t.avg_row_len ?? '-'} B</Descriptions.Item>
            <Descriptions.Item label="PCT_FREE">{t.pct_free ?? '-'}</Descriptions.Item>
            <Descriptions.Item label="最后分析">{t.last_analyzed ? new Date(t.last_analyzed).toLocaleString() : '未收集'}</Descriptions.Item>
            <Descriptions.Item label="陈旧原因" span={2}>
              {t.staleness_reason ? (
                <Text type="danger">{t.staleness_reason}</Text>
              ) : (
                <Tag color="success">统计新鲜</Tag>
              )}
            </Descriptions.Item>
            {(t.inserts != null || t.updates != null || t.deletes != null) && (
              <Descriptions.Item label="自上次收集 DML" span={2}>
                INS: {t.inserts ?? 0} · UPD: {t.updates ?? 0} · DEL: {t.deletes ?? 0}
              </Descriptions.Item>
            )}
          </Descriptions>
        </Card>
      ))}
    </div>
  );
}

function IndexesPanel({ indexes, tables }: { indexes: IndexProfileOut[]; tables: TableProfileOut[] }) {
  if (indexes.length === 0) return <Alert type="warning" message="无相关索引" showIcon />;
  const tableRowMap = new Map(tables.map(t => [`${t.owner}.${t.table_name}`, t.num_rows]));
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {indexes.map((idx, i) => {
        const tblRows = tableRowMap.get(`${idx.owner}.${idx.table_name}`);
        const cfRatio = idx.clustering_factor && tblRows ? idx.clustering_factor / tblRows : null;
        return (
          <Card key={i} size="small" title={
            <Space>
              <Tag color="orange">{idx.owner}</Tag>
              <strong>{idx.index_name}</strong>
              {idx.uniqueness === 'UNIQUE' && <Tag color="green">UNIQUE</Tag>}
              {idx.used === true && <Tag color="blue">✓ 被使用</Tag>}
              {idx.used === false && idx.usage_monitoring && <Tag color="default">未使用</Tag>}
            </Space>
          } extra={<Text type="secondary">ON {idx.table_name}</Text>}>
            <Descriptions column={2} size="small" bordered>
              <Descriptions.Item label="类型">{idx.index_type ?? '-'}</Descriptions.Item>
              <Descriptions.Item label="列数">{idx.column_count}</Descriptions.Item>
              <Descriptions.Item label="叶子块">{idx.leaf_blocks?.toLocaleString() ?? '-'}</Descriptions.Item>
              <Descriptions.Item label="DISTINCT_KEYS">{idx.distinct_keys?.toLocaleString() ?? '-'}</Descriptions.Item>
              <Descriptions.Item label="CLUSTERING_FACTOR">
                {idx.clustering_factor?.toLocaleString() ?? '-'}
                {cfRatio != null && (
                  <Tag color={cfRatio > 0.8 ? 'red' : cfRatio > 0.5 ? 'orange' : 'success'} style={{ marginLeft: 4 }}>
                    {cfRatio.toFixed(2)} · {cfRatio > 0.8 ? '回表代价高' : cfRatio > 0.5 ? '一般' : '良好'}
                  </Tag>
                )}
              </Descriptions.Item>
              <Descriptions.Item label="索引行数">{idx.num_rows?.toLocaleString() ?? '-'}</Descriptions.Item>
              <Descriptions.Item label="最后分析">{idx.last_analyzed ? new Date(idx.last_analyzed).toLocaleString() : '-'}</Descriptions.Item>
              <Descriptions.Item label="列顺序" span={2}>
                {idx.columns.length > 0 ? (
                  <Space wrap>
                    {idx.columns.sort((a, b) => a.column_position - b.column_position).map(c => (
                      <Tag key={c.column_name} color="geekblue">
                        {c.column_position}. {c.column_name}{c.descend === 'DESC' ? ' DESC' : ''}
                      </Tag>
                    ))}
                  </Space>
                ) : (
                  <Text type="secondary">无列信息（可能是函数索引）</Text>
                )}
              </Descriptions.Item>
            </Descriptions>
          </Card>
        );
      })}
    </div>
  );
}

function ColumnsPanel({ columns }: { columns: ColumnProfileOut[] }) {
  if (columns.length === 0) return <Alert type="warning" message="无列信息" showIcon />;
  const cols: ColumnsType<ColumnProfileOut> = [
    { title: '表', key: 'tbl', render: (_, r) => <Text type="secondary">{r.owner}.{r.table_name}</Text> },
    { title: '列', dataIndex: 'column_name', key: 'column_name', render: (v) => <strong>{v}</strong> },
    { title: '类型', key: 'type', render: (_, r) => `${r.data_type}(${r.data_length})${r.nullable === 'Y' ? ' NULL' : ''}` },
    { title: 'NULL%', key: 'null', width: 100, render: (_, r) => r.num_nulls != null && r.num_distinct != null
      ? ((r.num_nulls / Math.max(r.num_nulls + r.num_distinct, 1)) * 100).toFixed(1) + '%' : '-' },
    { title: '基数', dataIndex: 'num_distinct', key: 'nd', render: (v) => v?.toLocaleString() ?? '-' },
    { title: '直方图', key: 'hist', render: (_, r) => r.histogram && r.histogram !== 'NONE'
      ? <Tag color="blue">{r.histogram} ({r.histogram_buckets} buckets)</Tag>
      : <Text type="secondary">无</Text> },
    { title: '高值', dataIndex: 'high_value', key: 'hv', ellipsis: true },
  ];
  // Group by table
  const grouped = new Map<string, ColumnProfileOut[]>();
  columns.forEach(c => grouped.set(`${c.owner}.${c.table_name}`, [...(grouped.get(`${c.owner}.${c.table_name}`) || []), c]));
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {Array.from(grouped.entries()).map(([tbl, cols2]) => (
        <Card key={tbl} size="small" title={<Tag color="geekblue">{tbl}</Tag>}>
          <Table size="small" pagination={false} columns={cols} dataSource={cols2} rowKey="column_name" />
        </Card>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Diagnosis panel — Findings 卡片
// ---------------------------------------------------------------------------
const severityStyle: Record<string, { color: string; bg: string; label: string; dot: string }> = {
  极高: { color: '#fff', bg: '#cf1322', label: '极高', dot: 'error' },
  高:   { color: '#fff', bg: '#fa541c', label: '高',   dot: 'error' },
  中:   { color: '#fff', bg: '#d48806', label: '中',   dot: 'warning' },
  低:   { color: '#fff', bg: '#389e0d', label: '低',   dot: 'success' },
};

function DiagnosisPanel({ report }: { report: any }) {
  if (!report) {
    return <Alert type="info" message={'点击“一键分析”后此处显示诊断报告'} showIcon />;
  }
  const findings = report.findings || [];

  // Severity counts
  const counts: Record<string, number> = {};
  findings.forEach((f: any) => { counts[f.severity] = (counts[f.severity] || 0) + 1; });

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {/* Summary banner */}
      <Alert
        type={findings.length === 0 ? 'success' : findings.some((f: any) => f.severity === '极高') ? 'error' : findings.some((f: any) => f.severity === '高') ? 'warning' : 'info'}
        message={report.summary}
        showIcon
      />

      {/* Severity quick chips */}
      {findings.length > 0 && (
        <Space wrap>
          {Object.entries(severityStyle).map(([sev, style]) => {
            const n = counts[sev] || 0;
            if (n === 0) return null;
            return <Tag key={sev} color={style.bg} style={{ color: style.color }}>{sev}: {n}</Tag>;
          })}
        </Space>
      )}

      {/* Findings list */}
      {findings.length === 0 ? (
        <Alert type="success" message="✅ 未发现诊断问题" showIcon />
      ) : (
        <List
          itemLayout="vertical"
          size="small"
          dataSource={findings}
          renderItem={(f: any) => {
            const style = severityStyle[f.severity] || severityStyle.中;
            return (
              <List.Item key={f.rule_id} style={{ padding: '8px 0', borderBottom: '1px solid #f0f0f0' }}>
                <List.Item.Meta
                  avatar={
                    <div style={{
                      width: 28, height: 28, borderRadius: 4,
                      background: style.bg, color: style.color,
                      fontWeight: 700, fontSize: 11,
                      display: 'flex', alignItems: 'center', justifyContent: 'center',
                    }}>{f.rule_id}</div>
                  }
                  title={
                    <Space size={8}>
                      <strong>{f.rule_name}</strong>
                      <Tag color={style.bg} style={{ color: style.color }}>{f.severity}</Tag>
                      {f.object_name && <Text type="secondary" style={{ fontSize: 12 }}>· {f.object_name}</Text>}
                    </Space>
                  }
                  description={
                    <div style={{ fontSize: 12, color: '#555', lineHeight: 1.6 }}>
                      <div>{f.description}</div>
                      {f.suggestion_hint && (
                        <div style={{ marginTop: 6, padding: '6px 10px', background: '#fff7e6', borderRadius: 4, border: '1px solid #ffd591' }}>
                          💡 <Text style={{ color: '#d46b08' }}>{f.suggestion_hint}</Text>
                        </div>
                      )}
                      {f.evidence && Object.keys(f.evidence).length > 0 && (
                        <Text type="secondary" style={{ fontSize: 11 }}>
                          证据: {JSON.stringify(f.evidence)}
                        </Text>
                      )}
                    </div>
                  }
                />
              </List.Item>
            );
          }}
        />
      )}
      <SuggestionsPanel report={report} originalSql={originalSql || ''} instanceId={instanceId} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Suggestions panel — runnable SQL grouped by type
// ---------------------------------------------------------------------------
const riskStyle: Record<string, { bg: string; color: string; label: string }> = {
  low:      { bg: '#389e0d', color: '#fff', label: '低风险' },
  medium:   { bg: '#d48806', color: '#fff', label: '中风险' },
  high:     { bg: '#cf1322', color: '#fff', label: '高风险' },
  critical: { bg: '#820014', color: '#fff', label: '极高风险' },
};
const typeLabel: Record<string, string> = {
  hint:    'Hint',
  index:   'CREATE INDEX',
  stats:   'DBMS_STATS',
  rewrite: 'SQL 改写 / 参数',
};

function SuggestionsPanel({ report, originalSql, instanceId }: { report: any; originalSql: string; instanceId?: number }) {
  const { message } = App.useApp();
  const [runningKey, setRunningKey] = React.useState<string>('');
  const suggestions: any[] = report?.suggestions || [];
  if (suggestions.length === 0) {
    return null;
  }
  // Group by type
  const grouped: Record<string, any[]> = {};
  suggestions.forEach((s: any) => {
    (grouped[s.suggestion_type] = grouped[s.suggestion_type] || []).push(s);
  });

  const copy = (sql: string) => {
    navigator.clipboard.writeText(sql);
    message.success('SQL 已复制到剪贴板');
  };

  return (
    <div style={{ marginTop: 20 }}>
      <Divider orientation="left" style={{ marginTop: 0 }}>
        <Tag color="geekblue" style={{ fontSize: 14, padding: '2px 10px' }}>
          💡 优化建议 · {suggestions.length} 条可执行 SQL
        </Tag>
      </Divider>
      {Object.entries(grouped).map(([type, items]) => (
        <div key={type} style={{ marginBottom: 16 }}>
          <Text strong style={{ fontSize: 13, color: '#1890ff', marginBottom: 8, display: 'block' }}>
            {typeLabel[type] || type} ({items.length})
          </Text>
          <Row gutter={[12, 12]}>
            {items.map((s, i) => {
              const risk = riskStyle[s.risk] || riskStyle.medium;
              return (
                <Col key={i} span={24}>
                  <Card
                    size="small"
                    style={{ borderLeft: `4px solid ${risk.bg}` }}
                    title={
                      <Space size={8}>
                        <strong>{s.title}</strong>
                        <Tag style={{ background: risk.bg, color: risk.color, margin: 0 }}>{risk.label}</Tag>
                        <Tag>{s.rule_id} {s.rule_name}</Tag>
                      </Space>
                    }
                    extra={
                      <Space size={4}>
                        <Button size="small" onClick={() => copy(s.runnable_sql)}>📋 复制</Button>
                        <Button size="small" type="primary" loading={runningKey === `${s.rule_id}-${i}`}
  onClick={async () => {
    if (!originalSql) { message.warning('请先提供 SQL'); return; }
    if (!instanceId) { message.warning('请先选择实例'); return; }
    const key = `${s.rule_id}-${i}`;
    setRunningKey(key);
    try {
      const afterSql = s.suggestion_type === 'hint' ? originalSql.replace(/SELECT/i, (m) => m + ' ' + s.runnable_sql.trim()) : s.runnable_sql;
      const resp = await fetch('/api/v1/sandbox/validate', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ instance_id: instanceId, before_sql: originalSql, after_sql: afterSql }),
      });
      const data = await resp.json();
      if (!resp.ok) { message.error(data.detail || '沙箱执行失败'); return; }
      Modal.info({
        title: data.overall_summary, width: 720,
        content: (
          <div>
            {Object.entries(data.delta).map(([metric, v]: any) => (
              <div key={metric} style={{ display: 'flex', justifyContent: 'space-between', padding: '4px 0', borderBottom: '1px solid #f0f0f0', fontSize: 12 }}>
                <strong>{metric}</strong>
                <span>
                  before: <Text code>{String(v.before)}</Text> → after: <Text code>{String(v.after)}</Text>
                  {v.pct_change !== null && (
                    <Text style={{ color: v.improved ? '#389e0d' : '#d46b08', marginLeft: 8 }}>
                      ({v.pct_change > 0 ? '+' : ''}{v.pct_change}%{v.improved === true ? ' ✓' : v.improved === false ? ' ✗' : ''})
                    </Text>
                  )}
                </span>
              </div>
            ))}
          </div>
        ),
      });
    } catch (e: any) { message.error('网络错误: ' + e.message); }
    finally { setRunningKey(''); }
  }}>▶ 在沙箱运行</Button>
                      </Space>
                    }
                  >
                    {s.description && <div style={{ fontSize: 12, color: '#555', marginBottom: 8 }}>{s.description}</div>}
                    <pre style={{
                      background: '#0f172a', color: '#e2e8f0', padding: 10, borderRadius: 4,
                      fontSize: 11, lineHeight: 1.55, overflow: 'auto', margin: 0, maxHeight: 200,
                      fontFamily: 'Menlo, Consolas, monospace',
                    }}>{s.runnable_sql}</pre>
                    {(s.estimated_benefit || s.side_effects) && (
                      <div style={{ marginTop: 8, display: 'flex', gap: 16, fontSize: 11 }}>
                        {s.estimated_benefit && <Text style={{ color: '#389e0d' }}>✓ {s.estimated_benefit}</Text>}
                        {s.side_effects && <Text style={{ color: '#d46b08' }}>⚠ {s.side_effects}</Text>}
                      </div>
                    )}
                  </Card>
                </Col>
              );
            })}
          </Row>
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Auxiliary diagnosis — locks / waits / parameters
// ---------------------------------------------------------------------------
function AuxiliaryPanel({ snapshot, loading, instanceId }: { snapshot: any; loading: boolean; instanceId: number | null }) {
  const [subTab, setSubTab] = React.useState<'waits' | 'locks' | 'params'>('waits');
  if (!instanceId) return <Text type="secondary">请先选择 Oracle 实例</Text>;
  if (loading) return <div>加载中...</div>;
  if (!snapshot) return <Text type="secondary">点击"辅助诊断"Tab 触发拉取</Text>;
  if (snapshot.error) return (
    <Alert type="warning" message="辅助诊断暂不可用"
           description={snapshot.error} showIcon style={{ marginTop: 8 }} />
  );

  const { top_waits = [], lock_waiters = [], parameters = [] } = snapshot;
  return (
    <div>
      <Space style={{ marginBottom: 12 }}>
        <Button size="small" type={subTab === 'waits' ? 'primary' : 'default'} onClick={() => setSubTab('waits')}>Waits ({top_waits.length})</Button>
        <Button size="small" type={subTab === 'locks' ? 'primary' : 'default'} onClick={() => setSubTab('locks')}>Locks ({lock_waiters.length})</Button>
        <Button size="small" type={subTab === 'params' ? 'primary' : 'default'} onClick={() => setSubTab('params')}>Params ({parameters.length})</Button>
      </Space>

      {subTab === 'waits' && (
        <Table size="small" pagination={false} rowKey="event"
               columns={[
                 { title: 'Event', dataIndex: 'event', width: 220 },
                 { title: 'Waiters', dataIndex: 'wait_count', width: 90, render: (v: number) => <Tag color={v > 20 ? 'red' : v > 5 ? 'orange' : 'blue'}>{v}</Tag> },
                 { title: 'Total Wait (s)', dataIndex: 'total_wait_sec', width: 130, render: (v: number) => v?.toFixed(1) },
                 { title: '解读', dataIndex: 'interpretation', render: (t: string) => <Text type="secondary" style={{ fontSize: 12 }}>{t}</Text> },
               ]}
               dataSource={top_waits}
               locale={{ emptyText: '当前无等待会话' }} />
      )}

      {subTab === 'locks' && (
        <Table size="small" pagination={false} rowKey={(r: any) => r.sid + '-' + r.serial}
               columns={[
                 { title: 'SID:SERIAL', width: 110, render: (_: any, r: any) => <Text code>{r.sid}:{r.serial}</Text> },
                 { title: 'User', dataIndex: 'username', width: 90 },
                 { title: 'Machine', dataIndex: 'machine', width: 120, ellipsis: true },
                 { title: 'Lock', dataIndex: 'lock_type', width: 70 },
                 { title: 'Hold', dataIndex: 'mode_held', width: 80 },
                 { title: 'Wait', dataIndex: 'request_mode', width: 80 },
                 { title: 'Wait Event', dataIndex: 'wait_event', width: 200, ellipsis: true },
                 { title: 'SQL', dataIndex: 'sql_text_preview', width: 200, ellipsis: true, render: (t: string) => <Text code style={{ fontSize: 11 }}>{t}</Text> },
               ]}
               dataSource={lock_waiters}
               locale={{ emptyText: '当前无锁等待 (良好)' }} />
      )}

      {subTab === 'params' && (
        <Descriptions column={1} bordered size="small">
          {parameters.map((p: any) => (
            <Descriptions.Item key={p.name} label={
              <Space size={6}>
                <Text code>{p.name}</Text>
                {p.default ? <Tag color="default">default</Tag> : <Tag color="blue">modified</Tag>}
              </Space>
            }>
              <Text strong>{p.value || '-'}</Text>
              {p.description && <Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>{p.description}</Text>}
            </Descriptions.Item>
          ))}
          {parameters.length === 0 && <Text type="secondary">无参数数据</Text>}
        </Descriptions>
      )}
    </div>
  );
}
