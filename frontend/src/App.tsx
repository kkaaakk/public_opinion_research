import { useQuery } from '@tanstack/react-query';
import {
  AudioLines,
  Clock3,
  Database,
  MessageSquare,
  PanelLeftClose,
  PanelLeftOpen,
  Plus,
  Settings2,
} from 'lucide-react';
import { lazy, Suspense, useState } from 'react';
import { Link, NavLink, Outlet, Route, Routes, useLocation } from 'react-router';
import { ApiProvider, useApi } from '@/api/auth';
import { Loading, LinkButton } from '@/components/shared';
import { Button } from '@/components/ui/button';

const NewResearch = lazy(() => import('@/pages/NewResearch'));
const RunPage = lazy(() => import('@/pages/RunPage'));
const HistoryPage = lazy(() => import('@/pages/HistoryPage'));
const SessionPage = lazy(() => import('@/pages/SessionPage'));
const ExecutionPage = lazy(() => import('@/pages/ExecutionPage'));
const KnowledgePage = lazy(() => import('@/pages/KnowledgePage'));
const SettingsPage = lazy(() => import('@/pages/SettingsPage'));

function Layout() {
  const { api, authenticated } = useApi();
  const location = useLocation();
  const [open, setOpen] = useState(() => window.matchMedia('(min-width: 780px)').matches);
  const sessions = useQuery({
    queryKey: ['sessions', 0, 8],
    queryFn: ({ signal }) => api.sessions(0, 8, signal),
    enabled: authenticated,
  });
  const closeMobile = () => {
    if (window.matchMedia('(max-width: 779px)').matches) setOpen(false);
  };
  const path = location.pathname;
  return (
    <div className="app-shell" data-sidebar={open ? 'open' : 'closed'}>
      <aside className="sidebar" aria-label="侧边导航" hidden={!open}>
        <div className="branding">
          <Link to="/" className="brand" onClick={closeMobile}>
            <span className="brand-icon">
              <AudioLines size={20} strokeWidth={2.15} />
            </span>
            舆情研究
          </Link>
          <Button
            variant="ghost"
            size="icon"
            aria-label="收起侧边栏"
            onClick={() => setOpen(false)}
          >
            <PanelLeftClose />
          </Button>
        </div>
        <NavLink className="new-nav" end to="/" onClick={closeMobile}>
          <Plus size={18} />
          新建研究
        </NavLink>
        <div className="session-nav">
          <NavLink className="session-label" to="/sessions" onClick={closeMobile}>
            研究会话
          </NavLink>
          {authenticated && sessions.isPending && (
            <span className="sidebar-empty">正在读取会话…</span>
          )}
          {sessions.data?.items.map((session) => (
            <Link
              key={session.research_session_id}
              to={'/sessions/' + session.research_session_id}
              className="session-link"
              aria-current={
                path === '/sessions/' + session.research_session_id ||
                path === '/runs/' + session.latest_run_id
                  ? 'page'
                  : undefined
              }
              onClick={closeMobile}
              title={session.topic}
            >
              <MessageSquare size={15} />
              <span>{session.topic || '研究会话'}</span>
            </Link>
          ))}
          {sessions.data?.items.length === 0 && <span className="sidebar-empty">暂无研究会话</span>}
        </div>
        <nav className="secondary-nav" aria-label="工作台">
          <NavLink to="/history" onClick={closeMobile}>
            <Clock3 size={18} />
            研究历史
          </NavLink>
          <NavLink to="/knowledge" onClick={closeMobile}>
            <Database size={18} />
            企业知识库
          </NavLink>
          <NavLink to="/settings" onClick={closeMobile}>
            <Settings2 size={18} />
            系统设置
          </NavLink>
        </nav>
      </aside>
      {open && <button className="sidebar-scrim" aria-label="收起侧边栏" onClick={closeMobile} />}
      <div className="workspace">
        <header className="app-bar">
          {!open && (
            <Button
              variant="ghost"
              size="icon"
              aria-label="展开侧边栏"
              onClick={() => setOpen(true)}
            >
              <PanelLeftOpen />
            </Button>
          )}
          <Link to="/" className="app-bar-title">
            研究工作台
          </Link>
          {!authenticated && (
            <LinkButton variant="ghost" to="/settings" className="ml-auto">
              设置访问令牌
            </LinkButton>
          )}
        </header>
        <main id="main-content">
          <Outlet />
        </main>
      </div>
    </div>
  );
}

export default function App() {
  return (
    <ApiProvider>
      <Suspense fallback={<Loading text="正在载入工作台…" />}>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<NewResearch />} />
            <Route path="runs/:runId" element={<RunPage />} />
            <Route path="runs/:runId/execution" element={<ExecutionPage />} />
            <Route path="sessions" element={<SessionPage />} />
            <Route path="sessions/:sessionId" element={<SessionPage />} />
            <Route path="history" element={<HistoryPage />} />
            <Route path="history/:runId" element={<RunPage history />} />
            <Route path="knowledge" element={<KnowledgePage />} />
            <Route path="settings" element={<SettingsPage />} />
            <Route
              path="*"
              element={
                <div className="empty-content">
                  <p>未找到页面。</p>
                  <LinkButton to="/">返回工作台</LinkButton>
                </div>
              }
            />
          </Route>
        </Routes>
      </Suspense>
    </ApiProvider>
  );
}
