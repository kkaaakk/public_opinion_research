import { useMutation, useQuery } from '@tanstack/react-query';
import { RefreshCw } from 'lucide-react';
import { useState } from 'react';
import { useApi } from '@/api/auth';
import { Api, ApiError, type Schema } from '@/api/client';
import { ErrorNotice, Loading, PageTitle } from '@/components/shared';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { roleNames, sizeLabel } from '@/lib/presentation';

const models: Record<string, string> = {
  research_model: '研究',
  compression_model: '角色报告压缩',
  final_report_model: '最终报告',
  summarization_model: '摘要',
  rolling_compaction_model: '上下文压缩',
  rag_query_rewrite_model: '检索改写',
  rag_vision_model: 'Vision',
};
const database = { reachable: '可连接', unconfigured: '未配置', unavailable: '无法连接' };
const catalog = { readable: '可读取', unknown: '未知', unavailable: '无法读取' };
type SettingRow = [string, string | number | null | undefined];

function SettingsSummary({
  settings,
  knowledge,
}: {
  settings: Schema['Settings'];
  knowledge?: Schema['KnowledgeStatus'];
}) {
  const budget = settings.budget;
  const rows: SettingRow[] = [
    ['默认检索模式', settings.retrieval_mode],
    ['默认搜索服务', settings.search_api],
    ['启用角色', settings.enabled_roles.map((role) => roleNames[role] ?? role).join('、') || '无'],
    ['启用插件', settings.enabled_plugins.join('、') || '无'],
    ['允许澄清', settings.allow_clarification ? '是' : '否'],
    ['需要审批', settings.require_approval ? '是' : '否'],
    ['研究轮次上限', settings.max_research_rounds],
    ['每次 Agent 运行的轮次上限', settings.max_react_tool_calls],
    ['阶段超时', settings.workflow_step_timeout_seconds + ' 秒'],
    ['追踪', settings.tracing_enabled ? '已启用' : '已关闭'],
  ];
  const rag: SettingRow[] = [
    ['全局 RAG', settings.rag_enabled ? '已启用' : '已关闭'],
    ['Web 新建研究的 RAG 默认值', settings.new_run_rag_default ? '已启用' : '已关闭'],
    [
      '研究图片',
      settings.image_limits.enabled
        ? settings.image_limits.max_images +
          ' 张，每张 ' +
          sizeLabel(settings.image_limits.max_bytes)
        : '已关闭',
    ],
    ...(knowledge
      ? ([
          ['向量存储', knowledge.vector_store],
          ['Embedding', knowledge.embedding_model],
          ['Reranker', knowledge.reranker_model],
          ['多模态', knowledge.multimodal_enabled ? knowledge.multimodal_provider : '已关闭'],
        ] satisfies SettingRow[])
      : []),
  ];
  return (
    <>
      <section className="plain-section">
        <h2>运行配置</h2>
        <Definitions rows={rows} />
      </section>
      <section className="plain-section">
        <h2>模型配置</h2>
        <Definitions
          rows={Object.entries(settings.models).map(([key, value]) => [models[key] ?? key, value])}
        />
      </section>
      <section className="plain-section">
        <h2>RAG 配置</h2>
        <Definitions rows={rag} />
      </section>
      <section className="plain-section">
        <h2>预算配置</h2>
        <Definitions
          rows={[
            [
              '预算守卫',
              budget.budget_enabled == null ? '未知' : budget.budget_enabled ? '已启用' : '已关闭',
            ],
            ['模型调用', budget.max_model_calls],
            ['工具调用', budget.max_tool_calls],
            ['搜索调用', budget.max_search_calls],
            ['输入 Token', budget.max_input_tokens],
            ['输出 Token', budget.max_output_tokens],
          ]}
        />
      </section>
    </>
  );
}

function Definitions({ rows }: { rows: SettingRow[] }) {
  return (
    <dl className="definition-grid">
      {rows.map(([label, value]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd>
            {value == null ? '—' : typeof value === 'number' ? value.toLocaleString() : value}
          </dd>
        </div>
      ))}
    </dl>
  );
}

export default function SettingsPage() {
  const { api, authenticated, saveToken } = useApi();
  const [draft, setDraft] = useState('');
  const access = useMutation({
    mutationFn: async (token: string) => {
      await new Api(token).status(AbortSignal.timeout(12_000));
      return token;
    },
    onSuccess: saveToken,
  });
  const health = useQuery({ queryKey: ['health'], queryFn: ({ signal }) => api.health(signal) });
  const status = useQuery({
    queryKey: ['system'],
    queryFn: ({ signal }) => api.status(signal),
    enabled: authenticated,
  });
  const settings = useQuery({
    queryKey: ['settings'],
    queryFn: ({ signal }) => api.settings(signal),
    enabled: authenticated,
  });
  const knowledge = useQuery({
    queryKey: ['knowledge'],
    queryFn: ({ signal }) => api.knowledge(signal),
    enabled: authenticated,
  });
  const state = status.data;
  return (
    <div className="content-page settings-page">
      <PageTitle title="系统设置" />
      <section className="plain-section">
        <h2>Web API 访问令牌</h2>
        <p className="muted">
          使用服务端配置的 PUBLIC_OPINION_API_TOKEN。已验证的令牌保存在当前标签页会话中。
        </p>
        <form
          className="token-form"
          onSubmit={(event) => {
            event.preventDefault();
            if (draft.trim() && !access.isPending) access.mutate(draft.trim());
          }}
        >
          <Input
            type="password"
            name="access-token"
            autoComplete="off"
            aria-label="Web API 访问令牌"
            placeholder="输入访问令牌"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
          />
          <Button type="submit" disabled={access.isPending || !draft.trim()}>
            {access.isPending ? '验证中…' : '验证并保存'}
          </Button>
          {authenticated && (
            <Button variant="outline" type="button" onClick={() => saveToken('')}>
              清除令牌
            </Button>
          )}
        </form>
        {access.isError && <ErrorNotice error={access.error} />}
        {authenticated && (
          <p className="muted" role="status">
            认证
            {status.isPending
              ? '检查中'
              : status.isSuccess
                ? '有效'
                : status.error instanceof ApiError && status.error.status === 401
                  ? '无效'
                  : '暂不可确认'}
          </p>
        )}
      </section>
      <section className="plain-section">
        <div className="section-title">
          <h2>服务状态</h2>
          <Button
            variant="outline"
            onClick={() => {
              void health.refetch();
              if (authenticated) {
                void status.refetch();
                void settings.refetch();
                void knowledge.refetch();
              }
            }}
          >
            <RefreshCw data-icon="inline-start" />
            重新检查
          </Button>
        </div>
        <Definitions
          rows={[
            [
              'Web 服务',
              health.data?.status === 'ok' ? '在线' : health.isError ? '无法连接' : '检测中',
            ],
            ['PostgreSQL', state ? database[state.database] : authenticated ? '未知' : '需要认证'],
            [
              'DBOS 目录',
              state ? catalog[state.catalog ?? 'unknown'] : authenticated ? '未知' : '需要认证',
            ],
            ['Executor', state ? '当前部署无可验证的进程心跳' : '未知'],
            ...(state?.registered_queues
              ? [['已注册队列', state.registered_queues.join('、') || '无'] as [string, string]]
              : []),
          ]}
        />
        {status.isError && <ErrorNotice error={status.error} />}
      </section>
      {authenticated &&
        (settings.isError ? (
          <ErrorNotice error={settings.error} retry={() => void settings.refetch()} />
        ) : settings.data ? (
          <SettingsSummary settings={settings.data} knowledge={knowledge.data} />
        ) : (
          <Loading />
        ))}
    </div>
  );
}
