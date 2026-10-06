import React, { useEffect, useState } from 'react';
import { Table, Card, Tag, Button, Input, Select, Modal, Descriptions, Timeline, Space, Typography, Badge, message } from 'antd';
import { SearchOutlined, ReloadOutlined, DeleteOutlined, CheckCircleOutlined, CloseCircleOutlined } from '@ant-design/icons';

const { Text, Paragraph } = Typography;

const STATUS_COLORS: Record<string, string> = {
  pending: 'gold',
  running: 'processing',
  accepted: 'success',
  rejected: 'default',
  failed: 'error',
  cancelled: 'default',
};
const STATUS_LABELS: Record<string, string> = {
  pending: '待处理',
  running: '运行中',
  accepted: '已采纳',
  rejected: '已拒绝',
  failed: '失败',
  cancelled: '已取消',
};

export default function HistoryPage() {
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState<any[]>([]);
  const [filterStatus, setFilterStatus] = useState<string | undefined>();
  const [keyword, setKeyword] = useState('');
  const [detail, setDetail] = useState<any>(null);

  const load = async () => {
    setLoading(true);
    const params = new URLSearchParams();
    if (filterStatus) params.set('status', filterStatus);
    if (keyword) params.set('keyword', keyword);
    try {
      const resp = await fetch(`/api/v1/history?${params.toString()}`);
      const list = await resp.json();
      setData(Array.isArray(list) ? list : []);
    } catch (e: any) {
      message.error('加载失败: ' + e.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const openDetail = (row: any) => setDetail(row);

  const patchStatus = async (id: number, status: string) => {
    await fetch(`/api/v1/history/${id}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status }),
    });
    message.success(`已标记为 ${STATUS_LABELS[status]}`);
    setDetail({ ...detail, status });
    load();
  };

  const deleteRecord = async (id: number) => {
    if (!confirm('确定删除这条历史记录？')) return;
    await fetch(`/api/v1/history/${id}`, { method: 'DELETE' });
    message.success('已删除');
    setDetail(null);
    load();
  };

  const columns = [
    { title: '#', dataIndex: 'id', width: 60 },
    { title: '状态', dataIndex: 'status', width: 90, render: (s: string) =>
      <Tag color={STATUS_COLORS[s] || 'default'}>{STATUS_LABELS[s] || s}</Tag> },
    { title: '原始 SQL', dataIndex: 'original_sql', render: (sql: string) =>
      <Text code ellipsis style={{ maxWidth: 320, display: 'block' }}>{sql}</Text> },
    { title: '实例', dataIndex: 'instance_name', width: 120 },
    { title: '建议', dataIndex: 'suggestions', width: 70, render: (s: any[]) =>
      <Badge count={s?.length || 0} color="geekblue" /> },
    { title: '改进', dataIndex: 'improvement_pct', width: 80, render: (v: number) =>
      v != null ? <Text strong style={{ color: v > 0 ? '#389e0d' : '#cf1322' }}>
        {v > 0 ? '+' : ''}{v.toFixed(1)}%</Text> : '—' },
    { title: '操作人', dataIndex: 'operator', width: 100 },
    { title: '创建时间', dataIndex: 'created_at', width: 170, render: (t: string) => t?.replace('T', ' ').slice(0, 19) },
    {
      title: '操作', width: 200,
      render: (_: any, row: any) => (
        <Space size={4}>
          <Button size="small" type="link" onClick={() => openDetail(row)}>详情</Button>
          {row.status === 'pending' && (
            <>
              <Button size="small" type="link" onClick={() => patchStatus(row.id, 'accepted')}>采纳</Button>
              <Button size="small" type="link" danger onClick={() => patchStatus(row.id, 'rejected')}>拒绝</Button>
            </>
          )}
          <Button size="small" type="link" danger icon={<DeleteOutlined />} onClick={() => deleteRecord(row.id)} />
        </Space>
      ),
    },
  ];

  return (
    <Card title="📜 优化历史"
          extra={<Space>
            <Input.Search placeholder="SQL 关键字" allowClear style={{ width: 220 }}
                          onSearch={(k) => { setKeyword(k); load(); }} />
            <Select placeholder="状态" allowClear style={{ width: 130 }}
                    value={filterStatus} onChange={(v) => { setFilterStatus(v); load(); }}>
              {Object.entries(STATUS_LABELS).map(([v, l]) => <Select.Option key={v} value={v}>{l}</Select.Option>)}
            </Select>
            <Button icon={<ReloadOutlined />} onClick={load}>刷新</Button>
          </Space>}>
      <Table columns={columns} dataSource={data} rowKey="id" loading={loading}
             pagination={{ pageSize: 20, showTotal: (t) => `共 ${t} 条` }} size="middle" />
      {detail && <DetailModal record={detail} onClose={() => setDetail(null)}
                               onPatch={patchStatus} onDelete={deleteRecord} />}
    </Card>
  );
}

function DetailModal({ record, onClose, onPatch, onDelete }: any) {
  const [tab, setTab] = useState<string>('overview');
  return (
    <Modal open={!!record} onCancel={onClose} width={900} footer={null}
           title={<Space>
             <Tag color={STATUS_COLORS[record.status]}>{STATUS_LABELS[record.status]}</Tag>
             <span>优化历史 #{record.id}</span>
             <Text type="secondary">{record.operator} · {record.created_at?.replace('T', ' ').slice(0, 19)}</Text>
           </Space>}>
      <div style={{ marginBottom: 16 }}>
        <Space>
          <Button size="small" type={tab === 'overview' ? 'primary' : 'default'} onClick={() => setTab('overview')}>概览</Button>
          <Button size="small" type={tab === 'findings' ? 'primary' : 'default'} onClick={() => setTab('findings')}>诊断 ({record.findings?.length || 0})</Button>
          <Button size="small" type={tab === 'suggestions' ? 'primary' : 'default'} onClick={() => setTab('suggestions')}>建议 ({record.suggestions?.length || 0})</Button>
          <Button size="small" type={tab === 'metrics' ? 'primary' : 'default'} onClick={() => setTab('metrics')}>对比</Button>
          <Button size="small" type={tab === 'sql' ? 'primary' : 'default'} onClick={() => setTab('sql')}>SQL</Button>
        </Space>
      </div>

      {tab === 'overview' && (
        <Descriptions column={2} bordered size="small">
          <Descriptions.Item label="实例 ID">{record.instance_id}</Descriptions.Item>
          <Descriptions.Item label="SQL ID">{record.sql_id || '—'}</Descriptions.Item>
          <Descriptions.Item label="改进幅度">{record.improvement_pct != null ? `${record.improvement_pct.toFixed(1)}%` : '—'}</Descriptions.Item>
          <Descriptions.Item label="耗时">{record.finished_at ? '已完成' : '进行中'}</Descriptions.Item>
          <Descriptions.Item label="创建时间">{record.created_at}</Descriptions.Item>
          <Descriptions.Item label="完成时间">{record.finished_at || '—'}</Descriptions.Item>
        </Descriptions>
      )}

      {tab === 'findings' && (
        <div>
          {(record.findings || []).map((f: any, i: number) => (
            <Card size="small" key={i} style={{ marginBottom: 8, borderLeft: `3px solid ${severityColor(f.severity)}` }}>
              <Space size={8}>
                <Tag color="geekblue">{f.rule_id}</Tag>
                <Tag>{f.severity}</Tag>
                <strong>{f.rule_name}</strong>
              </Space>
              <Paragraph style={{ marginTop: 8, marginBottom: 0 }}>{f.description}</Paragraph>
              {f.object_name && <Text type="secondary" style={{ fontSize: 12 }}>对象: {f.object_name}</Text>}
            </Card>
          ))}
          {(!record.findings || record.findings.length === 0) && <Text type="secondary">无诊断发现</Text>}
        </div>
      )}

      {tab === 'suggestions' && (
        <div>
          {(record.suggestions || []).map((s: any, i: number) => (
            <Card size="small" key={i} style={{ marginBottom: 8, borderLeft: `3px solid ${riskColor(s.risk)}` }}
                  title={<Space><strong>{s.title}</strong><Tag>{s.suggestion_type}</Tag><Tag>{s.risk}</Tag></Space>}>
              {s.description && <Paragraph style={{ fontSize: 12, marginBottom: 8 }}>{s.description}</Paragraph>}
              {s.runnable_sql && (
                <pre style={{ background: '#0f172a', color: '#e2e8f0', padding: 10, borderRadius: 4, fontSize: 11, maxHeight: 180, overflow: 'auto', margin: 0 }}>
                  {s.runnable_sql}
                </pre>
              )}
            </Card>
          ))}
          {(!record.suggestions || record.suggestions.length === 0) && <Text type="secondary">无建议</Text>}
        </div>
      )}

      {tab === 'metrics' && (
        <div>
          {record.before_metrics && record.after_metrics ? (
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
              <thead><tr style={{ background: '#f5f5f5' }}>
                <th style={thStyle}>指标</th><th style={thStyle}>Before</th><th style={thStyle}>After</th><th style={thStyle}>Delta</th>
              </tr></thead>
              <tbody>{Object.keys(record.before_metrics).map(k => {
                const b = record.before_metrics[k], a = record.after_metrics?.[k];
                if (typeof b !== 'number') return null;
                const delta = a - b;
                return <tr key={k}>
                  <td style={tdStyle}>{k}</td>
                  <td style={tdStyle}><Text code>{b}</Text></td>
                  <td style={tdStyle}><Text code>{a}</Text></td>
                  <td style={tdStyle}><Text style={{ color: delta < 0 ? '#389e0d' : delta > 0 ? '#cf1322' : '#555' }}>
                    {delta > 0 ? '+' : ''}{delta}
                  </Text></td>
                </tr>;
              })}</tbody>
            </table>
          ) : <Text type="secondary">无对比数据（未跑过沙箱验证）</Text>}
        </div>
      )}

      {tab === 'sql' && (
        <div>
          <Text strong style={{ display: 'block', marginBottom: 4 }}>原始 SQL</Text>
          <pre style={{ background: '#0f172a', color: '#e2e8f0', padding: 10, borderRadius: 4, fontSize: 12, maxHeight: 180, overflow: 'auto' }}>
            {record.original_sql}
          </pre>
          {record.optimized_sql && (<>
            <Text strong style={{ display: 'block', margin: '12px 0 4px' }}>优化后 SQL</Text>
            <pre style={{ background: '#0f172a', color: '#a7f3d0', padding: 10, borderRadius: 4, fontSize: 12, maxHeight: 180, overflow: 'auto' }}>
              {record.optimized_sql}
            </pre>
          </>)}
        </div>
      )}

      <div style={{ marginTop: 16, borderTop: '1px solid #f0f0f0', paddingTop: 12, display: 'flex', gap: 8 }}>
        {record.status === 'pending' && (
          <>
            <Button type="primary" icon={<CheckCircleOutlined />} onClick={() => onPatch(record.id, 'accepted')}>采纳</Button>
            <Button danger icon={<CloseCircleOutlined />} onClick={() => onPatch(record.id, 'rejected')}>拒绝</Button>
          </>
        )}
        <Button onClick={() => onPatch(record.id, 'cancelled')}>取消</Button>
        <Button danger icon={<DeleteOutlined />} onClick={() => onDelete(record.id)}>删除</Button>
        <Button style={{ marginLeft: 'auto' }} onClick={onClose}>关闭</Button>
      </div>
    </Modal>
  );
}

const thStyle: React.CSSProperties = { padding: '8px 12px', border: '1px solid #e8e8e8', textAlign: 'left' };
const tdStyle: React.CSSProperties = { padding: '8px 12px', border: '1px solid #e8e8e8' };

function severityColor(sev?: string) {
  return ({ 极高: '#820014', 高: '#cf1322', 中: '#d48806', 低: '#52c41a' } as any)[sev || ''] || '#1677ff';
}
function riskColor(risk?: string) {
  return ({ critical: '#820014', high: '#cf1322', medium: '#d48806', low: '#52c41a' } as any)[risk || ''] || '#1677ff';
}
