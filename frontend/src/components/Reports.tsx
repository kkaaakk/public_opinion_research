import { useState } from 'react';
import { Download, FileText } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { ROLE_LABELS } from '../projection';
import { Empty, exportMarkdown } from './common';
export type ReportOption = { id: string; label: string; content: string };
export function Reports({
  reports,
  archive = false,
}: {
  reports: ReportOption[];
  archive?: boolean;
}) {
  const [selection, setSelection] = useState('final');
  const selected = reports.find((r) => r.id === selection) ?? reports.at(-1);
  return (
    <section className="panel report-panel">
      <div className="panel-head">
        <strong>
          <FileText size={16} />
          {archive ? 'Neo4j 报告归档' : '研究产物'}
        </strong>
        <div className="filters">
          <select
            aria-label="选择报告"
            disabled={!reports.length}
            value={selected?.id ?? ''}
            onChange={(e) => setSelection(e.target.value)}
          >
            {!reports.length && <option value="">尚无报告</option>}
            {reports.map((r) => (
              <option key={r.id} value={r.id}>
                {ROLE_LABELS[r.label] ?? r.label}
              </option>
            ))}
          </select>
          <button
            className="button small"
            disabled={!selected?.content}
            onClick={() => exportMarkdown(selected?.content ?? '', selected?.id)}
          >
            <Download size={14} />
            Markdown 导出
          </button>
        </div>
      </div>
      {archive && (
        <p className="panel-note">报告归档与 DBOS 执行事件独立。此视图不代表完整 Trace。</p>
      )}
      {selected?.content ? (
        <article className="markdown">
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            skipHtml
            components={{
              img: ({ alt }) => <span className="note">[图片：{alt ?? '未提供'}]</span>,
              a: ({ href, children }) =>
                href && /^https?:\/\//.test(href) ? (
                  <a href={href} target="_blank" rel="noreferrer">
                    {children}
                  </a>
                ) : (
                  <span>{children}</span>
                ),
            }}
          >
            {selected.content}
          </ReactMarkdown>
        </article>
      ) : (
        <Empty>尚无已发布报告。阶段产物会在角色完成后出现。</Empty>
      )}
    </section>
  );
}
