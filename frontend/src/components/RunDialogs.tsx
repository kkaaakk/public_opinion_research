import { useState, type FormEvent } from 'react';
import { Play } from 'lucide-react';
import type { RunRequest } from '../protocol';
import { Modal } from './common';
export const MODELS = [
  'deepseek:deepseek-flash',
  'deepseek:deepseek-chat',
  'deepseek:deepseek-v4-flash',
  'openai:gpt-4.1',
  'openai:gpt-4o',
];
export function CreateDialog({
  onClose,
  onCreate,
  busy,
}: {
  onClose: () => void;
  onCreate: (request: RunRequest) => void;
  busy: boolean;
}) {
  const [request, setRequest] = useState<RunRequest>({
    topic: '',
    model: MODELS[0],
    mode: 'normal',
    search_api: 'tavily',
    org_context: '',
    rag_enabled: true,
    require_approval: true,
    allow_clarification: true,
  });
  function submit(e: FormEvent) {
    e.preventDefault();
    if (request.topic.trim()) onCreate({ ...request, topic: request.topic.trim() });
  }
  return (
    <Modal title="新建研究" onClose={onClose}>
      <form className="research-form" onSubmit={submit}>
        <label>
          研究主题
          <textarea
            autoFocus
            required
            maxLength={12000}
            rows={4}
            value={request.topic}
            onChange={(e) => setRequest({ ...request, topic: e.target.value })}
            placeholder="描述要调查的品牌、企业、产品或舆情事件"
          />
        </label>
        <div className="form-grid">
          <label>
            研究模型
            <select
              value={request.model}
              onChange={(e) => setRequest({ ...request, model: e.target.value })}
            >
              {MODELS.map((m) => (
                <option key={m}>{m}</option>
              ))}
            </select>
          </label>
          <label>
            研究深度
            <select
              value={request.mode}
              onChange={(e) =>
                setRequest({ ...request, mode: e.target.value as RunRequest['mode'] })
              }
            >
              <option value="fast">Fast · 有界快速研究</option>
              <option value="normal">Normal · 标准研究</option>
              <option value="deep">Deep · 深度研究</option>
            </select>
          </label>
        </div>
        <label>
          组织背景（可选）
          <textarea
            rows={2}
            maxLength={8000}
            value={request.org_context}
            onChange={(e) => setRequest({ ...request, org_context: e.target.value })}
          />
        </label>
        <div className="form-checks">
          <label>
            <input
              type="checkbox"
              checked={request.rag_enabled}
              onChange={(e) => setRequest({ ...request, rag_enabled: e.target.checked })}
            />
            RAG 内部知识检索
          </label>
          <label>
            <input
              type="checkbox"
              checked={request.require_approval}
              onChange={(e) => setRequest({ ...request, require_approval: e.target.checked })}
            />
            执行前审批计划
          </label>
          <label>
            <input
              type="checkbox"
              checked={request.allow_clarification ?? true}
              onChange={(e) => setRequest({ ...request, allow_clarification: e.target.checked })}
            />
            必要时澄清研究需求
          </label>
        </div>
        <p className="note">
          搜索使用受保护 API 支持的 Tavily 工具。服务端可能强制计划审批。模型与工具调用由现有 DBOS /
          SDK 链路执行。
        </p>
        <div className="form-actions">
          <button type="button" className="button" onClick={onClose}>
            关闭
          </button>
          <button className="button primary" disabled={busy || !request.topic.trim()}>
            <Play size={14} />
            创建持久 Run
          </button>
        </div>
      </form>
    </Modal>
  );
}
