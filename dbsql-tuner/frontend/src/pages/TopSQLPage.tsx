import { useEffect, useMemo, useState } from 'react';
import { App, Button, Card, Col, Form, Input, Radio, Row, Select, Space, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import dayjs from 'dayjs';
import api from '../services/api';
import type { OracleInstance, TopSQLRow } from '../types';

const { Title } = Typography;

const metricOptions = [
  { label: '总耗时', value: 'elapsed' },
  { label: '总 CPU', value: 'cpu' },
  { label: '总逻辑读', value: 'logical_reads' },
  { label: '总物理读', value: 'physical_reads' },
  { label: '执行次数', value: 'executions' },
];

const fmtUS = (us?: number | null) => {
  if (!us) return '-';
  if (us >= 1e9) return (us / 1e9).toFixed(2) + ' s';
  if (us >= 1e6) return (us / 1e6).toFixed(2) + ' ms';
  if (us >= 1e3) return (us / 1e3).toFixed(2) + ' μs';
  return us + ' ns';
};

export default function TopSQLPage() {
  const { message } = App.useApp();
  const [instances, setInstances] = useState<OracleInstance[]>([]);
  const [loading, setLoading] = useState(false);
  const [rows, setRows] = useState<TopSQLRow[]>([]);
  const [form] = Form.useForm();

  useEffect(() => {
    api.get('/instances').then(r => setInstances(r.data)).catch(() => {});
  }, []);

  const runQuery = async () => {
    const vals = await form.validateFields();
    const url = vals.source === 'realtime' ? '/top-sql/realtime' : '/top-sql/awr';
    try {
      setLoading(true);
      const res = await api.get(url, { params: vals });
      setRows(res.data);
    } catch (e: any) {
      message.error(e.message);
    } finally {
      setLoading(false);
    }
  };

  const columns: ColumnsType<TopSQLRow> = [
    {
      title: '#', key: 'idx', width: 48,
      render: (_, __, i) => <Tag color={i < 3 ? 'red' : i < 10 ? 'orange' : 'default'}>{i + 1}</Tag>,
    },
    {
      title: 'SQL ID / Hash', key: 'id', width: 180,
      render: (_, r) => (
        <div>
          {r.sql_id ? <div style={{ fontFamily: 'monospace' }}>{r.sql_id}</div> : <Tag color="blue">10g</Tag>}
          {r.hash_value != null && (
            <div style={{ fontFamily: 'monospace', color: '#888', fontSize: 11 }}>
              HASH: {r.hash_value}
            </div>
          )}
        </div>
      ),
    },
    {
      title: 'SQL 文本', dataIndex: 'sql_text', key: 'sql_text', ellipsis: true, width: 320,
      render: (t: string | null) => t ? (
        <div title={t} className="sql-text" style={{ maxHeight: 80, overflow: 'hidden' }}>
          {t.substring(0, 200)}{t.length > 200 ? '…' : ''}
        </div>
      ) : '-',
    },
    { title: '执行次数', dataIndex: 'executions', key: 'executions', width: 100, render: v => v?.toLocaleString() ?? '-' },
    { title: '总耗时', key: 'elapsed', width: 120, sorter: (a, b) => (a.elapsed_time_us || 0) - (b.elapsed_time_us || 0), render: (_, r) => fmtUS(r.elapsed_time_us) },
    { title: '平均耗时', key: 'avg', width: 100, render: (_, r) => r.avg_elapsed_ms ? r.avg_elapsed_ms.toFixed(2) + ' ms' : '-' },
    { title: '总逻辑读', dataIndex: 'buffer_gets', key: 'buffer_gets', width: 110, render: v => v?.toLocaleString() ?? '-' },
    { title: '总物理读', dataIndex: 'disk_reads', key: 'disk_reads', width: 110, render: v => v?.toLocaleString() ?? '-' },
    { title: '来源', dataIndex: 'source', key: 'source', width: 80, render: v => v === 'realtime' ? <Tag color="green">实时</Tag> : <Tag color="blue">AWR</Tag> },
  ];

  const totals = useMemo(() => {
    const count = rows.length;
    const totalElapsed = rows.reduce((s, r) => s + (r.elapsed_time_us || 0), 0);
    const totalBuffer = rows.reduce((s, r) => s + (r.buffer_gets || 0), 0);
    return { count, totalElapsed, totalBuffer };
  }, [rows]);

  return (
    <div>
      <Title level={4} style={{ marginBottom: 16 }}>Top SQL 定位</Title>

      <Card style={{ marginBottom: 16 }}>
        <Form form={form} layout="inline" onFinish={runQuery} initialValues={{ source: 'realtime', metric: 'elapsed', limit: 20, hours: 1 }}>
          <Form.Item name="instance_id" label="Oracle 实例" rules={[{ required: true }]}>
            <Select
              style={{ width: 220 }}
              placeholder="选择实例"
              options={instances.map(i => ({ label: `${i.name}  (${i.host}:${i.port})`, value: i.id }))}
            />
          </Form.Item>
          <Form.Item name="source" label="数据来源">
            <Radio.Group>
              <Radio.Button value="realtime">实时 (V$SQL)</Radio.Button>
              <Radio.Button value="awr">AWR 历史</Radio.Button>
            </Radio.Group>
          </Form.Item>
          <Form.Item noStyle shouldUpdate={(prev, cur) => prev.source !== cur.source}>
            {({ getFieldValue }) => getFieldValue('source') === 'awr' && (
              <Form.Item name="hours" label="回溯窗口(h)">
                <Input type="number" min={1} max={168} style={{ width: 80 }} />
              </Form.Item>
            )}
          </Form.Item>
          <Form.Item name="metric" label="排序指标">
            <Select style={{ width: 140 }} options={metricOptions} />
          </Form.Item>
          <Form.Item name="limit" label="数量">
            <Input type="number" min={5} max={200} style={{ width: 80 }} />
          </Form.Item>
          <Form.Item>
            <Space>
              <Button type="primary" htmlType="submit" loading={loading}>查询</Button>
              <Button onClick={() => form.resetFields()}>重置</Button>
            </Space>
          </Form.Item>
        </Form>
      </Card>

      {rows.length > 0 && (
        <Row gutter={16} style={{ marginBottom: 16 }}>
          <Col span={6}><Card size="small" title="Top SQL 数量">{totals.count} 条</Card></Col>
          <Col span={6}><Card size="small" title="累计耗时">{fmtUS(totals.totalElapsed)}</Card></Col>
          <Col span={6}><Card size="small" title="累计逻辑读">{totals.totalBuffer.toLocaleString()}</Card></Col>
          <Col span={6}><Card size="small" title="当前时间">{dayjs().format('YYYY-MM-DD HH:mm:ss')}</Card></Col>
        </Row>
      )}

      <Table rowKey={(r, i) => `${r.sql_id || r.hash_value}-${i}`} loading={loading} columns={columns} dataSource={rows} size="middle" />
    </div>
  );
}
