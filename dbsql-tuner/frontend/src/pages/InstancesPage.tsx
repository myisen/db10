import { useEffect, useState } from 'react';
import { App, Button, Form, Input, InputNumber, Modal, Popconfirm, Space, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import api from '../services/api';
import type { OracleInstance, ConnectionTestResult } from '../types';

const { Title } = Typography;

export default function InstancesPage() {
  const { message } = App.useApp();
  const [data, setData] = useState<OracleInstance[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<OracleInstance | null>(null);
  const [form] = Form.useForm();
  const [testResult, setTestResult] = useState<ConnectionTestResult | null>(null);
  const [testingId, setTestingId] = useState<number | null>(null);

  const load = async () => {
    setLoading(true);
    try {
      const res = await api.get('/instances');
      setData(res.data);
    } catch (e: any) {
      message.error(e.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const openCreate = () => {
    setEditing(null);
    form.resetFields();
    setModalOpen(true);
  };
  const openEdit = (row: OracleInstance) => {
    setEditing(row);
    form.setFieldsValue({
      name: row.name, host: row.host, port: row.port, service_name: row.service_name,
      read_user: row.read_user, sandbox_user: row.sandbox_user,
      // passwords left blank — user must re-enter to change
    });
    setModalOpen(true);
  };

  const submit = async () => {
    const vals = await form.validateFields();
    try {
      if (editing) {
        await api.patch(`/instances/${editing.id}`, vals);
        message.success('已更新');
      } else {
        await api.post('/instances', vals);
        message.success('已创建');
      }
      setModalOpen(false);
      load();
    } catch (e: any) {
      message.error(e.message);
    }
  };

  const remove = async (id: number) => {
    try {
      await api.delete(`/instances/${id}`);
      message.success('已删除');
      load();
    } catch (e: any) { message.error(e.message); }
  };

  const testConnection = async (id: number) => {
    setTestingId(id); setTestResult(null);
    try {
      const res = await api.post(`/instances/${id}/test`);
      setTestResult(res.data);
    } catch (e: any) {
      message.error(e.message);
    } finally {
      setTestingId(null);
    }
  };

  const warm = async (id: number) => {
    try {
      await api.post(`/instances/${id}/warm`);
      message.success('连接池已预热');
      load();
    } catch (e: any) { message.error(e.message); }
  };

  const statusTag = (status: string) => {
    const map: Record<string, { color: string; label: string }> = {
      up: { color: 'green', label: '在线' },
      down: { color: 'red', label: '离线' },
      forbidden: { color: 'orange', label: '权限不足' },
      unknown: { color: 'default', label: '未知' },
    };
    const s = map[status] || { color: 'default', label: status };
    return <Tag color={s.color}>{s.label}</Tag>;
  };

  const columns: ColumnsType<OracleInstance> = [
    { title: '名称', dataIndex: 'name', key: 'name' },
    { title: '主机', key: 'host', render: (_, r) => `${r.host}:${r.port}` },
    { title: 'Service', dataIndex: 'service_name', key: 'service_name' },
    { title: '账号', dataIndex: 'read_user', key: 'read_user' },
    { title: '版本', dataIndex: 'oracle_version', key: 'oracle_version', render: (v) => v || '-' },
    { title: '状态', dataIndex: 'status', key: 'status', render: statusTag },
    {
      title: '操作', key: 'actions', render: (_, r) => (
        <Space size={4}>
          <Button size="small" loading={testingId === r.id} onClick={() => testConnection(r.id)}>测试连接</Button>
          <Button size="small" onClick={() => warm(r.id)}>预热</Button>
          <Button size="small" onClick={() => openEdit(r)}>编辑</Button>
          <Popconfirm title="确认删除此实例？" onConfirm={() => remove(r.id)}>
            <Button size="small" danger>删除</Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 16 }}>
        <Title level={4} style={{ margin: 0 }}>Oracle 实例管理</Title>
        <Button type="primary" onClick={openCreate}>新增实例</Button>
      </div>

      <Table rowKey="id" loading={loading} columns={columns} dataSource={data} />

      {testResult && (
        <Modal
          title="连接测试结果"
          open={!!testResult}
          onCancel={() => setTestResult(null)}
          footer={[<Button key="ok" onClick={() => setTestResult(null)}>关闭</Button>]}
          width={560}
        >
          <p><strong>{testResult.ok ? '✅ 成功' : '❌ 失败'}</strong> · {testResult.message}</p>
          {testResult.oracle_version && <p>Oracle 版本：{testResult.oracle_version}</p>}
          {testResult.permissions.length > 0 && (
            <div>
              <p>已具备权限：</p>
              <ul>{testResult.permissions.map(p => <li key={p}>✅ {p}</li>)}</ul>
            </div>
          )}
          {testResult.missing_permissions.length > 0 && (
            <div>
              <p>缺失权限：</p>
              <ul>{testResult.missing_permissions.map(p => <li key={p}>❌ {p}</li>)}</ul>
            </div>
          )}
        </Modal>
      )}

      <Modal
        title={editing ? '编辑实例' : '新增实例'}
        open={modalOpen}
        onOk={submit}
        onCancel={() => setModalOpen(false)}
        okText="保存"
        cancelText="取消"
        destroyOnClose
      >
        <Form form={form} layout="vertical" preserve={false}>
          <Form.Item name="name" label="名称" rules={[{ required: true }]}>
            <Input placeholder="prod-rac-node1" />
          </Form.Item>
          <Space style={{ width: '100%' }}>
            <Form.Item name="host" label="主机" rules={[{ required: true }]} style={{ width: '60%' }}>
              <Input placeholder="10.0.0.1" />
            </Form.Item>
            <Form.Item name="port" label="端口" initialValue={1521} style={{ width: '35%' }}>
              <InputNumber min={1} max={65535} style={{ width: '100%' }} />
            </Form.Item>
          </Space>
          <Form.Item name="service_name" label="Service Name" rules={[{ required: true }]}>
            <Input placeholder="ORCL" />
          </Form.Item>
          <Space style={{ width: '100%' }}>
            <Form.Item name="read_user" label="只读账号" rules={[{ required: true }]} style={{ width: '48%' }}>
              <Input />
            </Form.Item>
            <Form.Item name="read_password" label="只读密码" rules={editing ? [] : [{ required: true }]} style={{ width: '48%' }}>
              <Input.Password />
            </Form.Item>
          </Space>
          <Space style={{ width: '100%' }}>
            <Form.Item name="sandbox_user" label="沙箱账号（可选）" style={{ width: '48%' }}>
              <Input />
            </Form.Item>
            <Form.Item name="sandbox_password" label="沙箱密码" style={{ width: '48%' }}>
              <Input.Password />
            </Form.Item>
          </Space>
        </Form>
      </Modal>
    </div>
  );
}
