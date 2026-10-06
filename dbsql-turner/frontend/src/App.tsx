import { Layout, Menu, Typography } from 'antd';
import { Link, Routes, Route, useLocation } from 'react-router-dom';
import { DatabaseOutlined, ThunderboltOutlined, HistoryOutlined, CodeOutlined } from '@ant-design/icons';
import InstancesPage from './pages/InstancesPage';
import TopSQLPage from './pages/TopSQLPage';
import SQLWorkbenchPage from './pages/SQLWorkbenchPage';
import HistoryPage from './pages/HistoryPage';

const { Header, Sider, Content } = Layout;
const { Title } = Typography;

export default function App() {
  const loc = useLocation();
  const selected = loc.pathname.startsWith('/workbench') ? 'workbench' :
                  loc.pathname.startsWith('/top-sql') ? 'top-sql' :
                  loc.pathname.startsWith('/history') ? 'history' : 'instances';

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Header style={{ background: '#001529', display: 'flex', alignItems: 'center', padding: '0 24px' }}>
        <Title level={4} style={{ color: '#fff', margin: 0, flex: 1 }}>
          ⚡ DBSQL-Turner · Oracle SQL 优化平台
        </Title>
      </Header>
      <Layout>
        <Sider width={200} style={{ background: '#fff' }}>
          <Menu mode="inline" selectedKeys={[selected]} style={{ height: '100%', borderRight: 0 }}>
            <Menu.Item key="instances" icon={<DatabaseOutlined />}>
              <Link to="/">Oracle 实例</Link>
            </Menu.Item>
            <Menu.Item key="top-sql" icon={<ThunderboltOutlined />}>
              <Link to="/top-sql">Top SQL</Link>
            </Menu.Item>
            <Menu.Item key="workbench" icon={<CodeOutlined />}>
              <Link to="/workbench">SQL 工作台</Link>
            </Menu.Item>
            <Menu.Item key="history" icon={<HistoryOutlined />}>
              <Link to="/history">优化历史</Link>
            </Menu.Item>
          </Menu>
        </Sider>
        <Layout style={{ padding: '24px' }}>
          <Content style={{ background: '#fff', padding: 24, borderRadius: 8, minHeight: 280 }}>
            <Routes>
              <Route path="/" element={<InstancesPage />} />
              <Route path="/top-sql" element={<TopSQLPage />} />
              <Route path="/workbench" element={<SQLWorkbenchPage />} />
              <Route path="/history" element={<HistoryPage />} />
            </Routes>
          </Content>
        </Layout>
      </Layout>
    </Layout>
  );
}
